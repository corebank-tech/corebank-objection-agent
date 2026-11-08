"""이의제기 Agent 평가 실행. corebank-server 를 평가 전용 DB 에 띄운 로컬 환경에서 돈다(eval/README.md).

run_eval.py register   사건과 이의제기 접수(첫 분석이 대기열에 들어간다)
run_eval.py drive N    워커가 처리하는 동안 건마다 실행이 N회가 되도록 재분석을 요청한다
run_eval.py collect    결과 수집 → results.json
"""

import json
import os
import subprocess
import sys
import time
import uuid
from pathlib import Path

import httpx

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))
BASE = os.environ.get("EVAL_SERVER_URL", "http://localhost:8080/api/v1")
MYSQL_CONTAINER = os.environ.get("EVAL_MYSQL_CONTAINER", "objection-demo-mysql")
MYSQL_PASSWORD = os.environ.get("EVAL_MYSQL_PASSWORD", "localpw")
ADMIN_PASSWORD = os.environ.get("EVAL_ADMIN_PASSWORD", "Admin1234!")  # 서버 local 프로필 시드 계정
# 다른 세트(홀드아웃 등)는 EVAL_MANIFEST 와 EVAL_TAG 로 고른다. 접수 번호 파일이 세트마다 따로 생긴다
MANIFEST_PATH = Path(os.environ.get("EVAL_MANIFEST", HERE / "manifest.json"))
# 이 번호보다 큰 실행만 센다. 같은 접수 건으로 설정(추론 수준 등)을 바꿔 다시 잴 때 앞선 실행과 섞지 않는다
MIN_RUN_ID = int(os.environ.get("EVAL_MIN_RUN_ID", "0"))
MANIFEST = json.loads(MANIFEST_PATH.read_text())
OBJECTIONS = HERE / (f"objections-{os.environ['EVAL_TAG']}.json" if os.environ.get("EVAL_TAG") else "objections.json")


def sql(query: str) -> list[str]:
    out = subprocess.run(
        [
            "docker",
            "exec",
            MYSQL_CONTAINER,
            "mysql",
            "-uroot",
            f"-p{MYSQL_PASSWORD}",
            "minicore",
            "-N",
            "--raw",
            "--default-character-set=utf8mb4",
            "-e",
            query,
        ],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return [line for line in out.split("\n") if line.strip()]


class Admin:
    def __init__(self):
        self.http = httpx.Client(base_url=BASE, timeout=30)
        self.login()

    def login(self):
        r = self.http.post("/admin/auth/login", json={"userId": "adminoperator", "password": ADMIN_PASSWORD})
        if r.json()["code"] != "0000":
            raise RuntimeError(f"관리자 로그인 실패: {r.text}")

    def upload(self, objection_id: int, documents: list[dict]):
        files = [("files", (name, content, "image/png")) for name, content in render_documents(documents)]
        r = self.http.post(
            f"/admin/objections/{objection_id}/documents",
            data={"kind": "EVIDENCE"},
            files=files,
            headers={"X-XSRF-TOKEN": self.http.cookies.get("XSRF-TOKEN") or "", "Idempotency-Key": str(uuid.uuid4())},
        )
        if r.json().get("code") != "0000":
            raise RuntimeError(f"서류 업로드 실패 {objection_id}: {r.text}")

    def post(self, path, body=None):
        for _ in range(2):
            r = self.http.post(
                path,
                json=body,
                headers={
                    "X-XSRF-TOKEN": self.http.cookies.get("XSRF-TOKEN") or "",
                    "Idempotency-Key": str(uuid.uuid4()),
                },
            )
            if r.status_code == 401:
                self.login()
                continue
            return r.json()
        raise RuntimeError(path)


def render_documents(documents: list[dict]) -> list[tuple[str, bytes]]:
    import io

    import documents as generator

    font = generator.find_font()
    files = []
    for i, doc in enumerate(documents, start=1):
        buffer = io.BytesIO()
        generator.render(doc["title"], doc["lines"], font).save(buffer, "PNG")
        files.append((f"evidence-{i}.png", buffer.getvalue()))
    return files


def register():
    admin = Admin()
    accounts = dict(
        row.split("\t")
        for row in sql(
            "SELECT account_number, account_id FROM account WHERE account_number IN ("
            + ",".join(f"'{s['account']}'" for s in MANIFEST)
            + ")"
        )
    )
    ids = {}
    for s in MANIFEST:
        case = admin.post(
            "/admin/payment-stops",
            {
                "accountId": int(accounts[s["account"]]),
                "disputedTransactionNumbers": s["disputed"],
                "objectionDeadline": "2026-12-04",
            },
        )
        if case["code"] != "0000":
            raise RuntimeError(f"사건 등록 실패 {s['id']}: {case}")
        objection = admin.post(
            "/admin/objections",
            {
                "caseId": case["data"]["caseId"],
                "clause": s.get("clause", 1),
                "statement": s["statement"],
                "simplifiedRequested": s.get("simplified", False),
                "idCopyConfirmed": True,
                "litigationPending": False,
            },
        )
        if objection["code"] != "0000":
            raise RuntimeError(f"이의제기 접수 실패 {s['id']}: {objection}")
        ids[s["id"]] = objection["data"]["objectionId"]
        if s.get("documents"):
            admin.upload(ids[s["id"]], s["documents"])
    OBJECTIONS.write_text(json.dumps(ids, indent=1))
    print("registered", len(ids))


def drive(target: int):
    admin = Admin()
    ids = json.loads(OBJECTIONS.read_text())
    while True:
        rows = sql(
            "SELECT objection_id, COUNT(*), SUM(status IN ('COMPLETED','FAILED')), MAX(status IN ('QUEUED','RUNNING'))"
            f" FROM agent_run WHERE run_id > {MIN_RUN_ID} GROUP BY objection_id"
        )
        state = {int(r.split("\t")[0]): tuple(int(x or 0) for x in r.split("\t")[1:]) for r in rows}
        done = 0
        for oid in ids.values():
            total, finished, active = state.get(oid, (0, 0, 0))
            if finished >= target:
                done += 1
            elif not active and total < target:
                res = admin.post(f"/admin/objections/{oid}/review-requests")
                if res["code"] != "0000":
                    print("review request", oid, res["code"])
        print(time.strftime("%H:%M:%S"), f"{done}/{len(ids)} 완료", flush=True)
        if done == len(ids):
            return
        time.sleep(10)


def collect():
    ids = json.loads(OBJECTIONS.read_text())
    query = """
    SELECT JSON_OBJECT(
      'run', r.run_id, 'objection', r.objection_id, 'status', r.status, 'attempt', r.attempt, 'model', r.model,
      'cost', r.cost_micro_usd, 'inputTokens', r.input_tokens, 'outputTokens', r.output_tokens,
      'seconds', TIMESTAMPDIFF(MICROSECOND, r.started_at, r.ended_at) / 1000000, 'failure', r.failure_reason,
      'agentBranch', (SELECT d.branch FROM review_draft d WHERE d.run_id = r.run_id),
      'draft', (SELECT d.content FROM review_draft d WHERE d.run_id = r.run_id),
      'gate', (SELECT s.detail FROM agent_run_step s WHERE s.run_id = r.run_id AND s.type = 'GATE_EVALUATED' LIMIT 1),
      'submitted', (SELECT s.detail FROM agent_run_step s WHERE s.run_id = r.run_id AND s.type = 'DRAFT_SUBMITTED' ORDER BY s.seq DESC LIMIT 1),
      'tools', (SELECT JSON_ARRAYAGG(s.detail) FROM agent_run_step s WHERE s.run_id = r.run_id AND s.type = 'TOOL_CALL'),
      'failedSteps', (SELECT COUNT(*) FROM agent_run_step s WHERE s.run_id = r.run_id AND s.type = 'FAILED'),
      'statement', (SELECT o.statement FROM objection o WHERE o.objection_id = r.objection_id))
    FROM agent_run r WHERE r.run_id > {min_run} ORDER BY r.run_id""".replace("{min_run}", str(MIN_RUN_ID))
    runs = [json.loads(line) for line in sql(query)]
    for run in runs:
        for key in ("draft", "gate", "submitted"):
            if isinstance(run[key], str):
                run[key] = json.loads(run[key])
        run["tools"] = [json.loads(t) if isinstance(t, str) else t for t in (run["tools"] or [])]
    by_objection = {v: k for k, v in ids.items()}
    for run in runs:
        run["scenario"] = by_objection.get(run["objection"])
    # 이 세트의 접수 건 실행만 남기고 어느 정답 세트로 채점해야 하는지 표시한다
    runs = [dict(run, set=MANIFEST_PATH.stem) for run in runs if run["scenario"] is not None]
    (HERE / "results.json").write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    print("collected", len(runs))


if __name__ == "__main__":
    {"register": lambda: register(), "drive": lambda: drive(int(sys.argv[2])), "collect": lambda: collect()}[
        sys.argv[1]
    ]()
