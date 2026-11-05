"""이의제기 Agent 평가 실행. corebank-server 를 평가 전용 DB 에 띄운 로컬 환경에서 돈다(eval/README.md).

  run_eval.py register   사건과 이의제기 접수(첫 분석이 대기열에 들어간다)
  run_eval.py drive N    워커가 처리하는 동안 건마다 실행이 N회가 되도록 재분석을 요청한다
  run_eval.py collect    결과 수집 → results.json
"""

import json
import subprocess
import sys
import time
import uuid
from pathlib import Path

import os

import httpx

HERE = Path(__file__).parent
BASE = os.environ.get("EVAL_SERVER_URL", "http://localhost:8080/api/v1")
MYSQL_CONTAINER = os.environ.get("EVAL_MYSQL_CONTAINER", "objection-demo-mysql")
MYSQL_PASSWORD = os.environ.get("EVAL_MYSQL_PASSWORD", "localpw")
ADMIN_PASSWORD = os.environ.get("EVAL_ADMIN_PASSWORD", "Admin1234!")  # 서버 local 프로필 시드 계정
MANIFEST = json.loads((HERE / "manifest.json").read_text())


def sql(query: str) -> list[str]:
    out = subprocess.run(
        ["docker", "exec", MYSQL_CONTAINER, "mysql", "-uroot", f"-p{MYSQL_PASSWORD}", "minicore", "-N", "--raw",
         "--default-character-set=utf8mb4", "-e", query],
        capture_output=True, text=True, check=True).stdout
    return [line for line in out.split("\n") if line.strip()]


class Admin:
    def __init__(self):
        self.http = httpx.Client(base_url=BASE, timeout=30)
        self.login()

    def login(self):
        r = self.http.post("/admin/auth/login", json={"userId": "adminoperator", "password": ADMIN_PASSWORD})
        assert r.json()["code"] == "0000", r.text

    def post(self, path, body=None):
        for _ in range(2):
            r = self.http.post(path, json=body, headers={
                "X-XSRF-TOKEN": self.http.cookies.get("XSRF-TOKEN") or "", "Idempotency-Key": str(uuid.uuid4())})
            if r.status_code == 401:
                self.login()
                continue
            return r.json()
        raise RuntimeError(path)


def register():
    admin = Admin()
    accounts = dict(row.split("\t") for row in sql("SELECT account_number, account_id FROM account WHERE account_number LIKE '0898%'"))
    ids = {}
    for s in MANIFEST:
        case = admin.post("/admin/payment-stops", {
            "accountId": int(accounts[s["account"]]), "disputedTransactionNumbers": s["disputed"], "objectionDeadline": "2026-12-04"})
        assert case["code"] == "0000", (s["id"], case)
        objection = admin.post("/admin/objections", {
            "caseId": case["data"]["caseId"], "clause": 1, "statement": s["statement"],
            "simplifiedRequested": False, "idCopyConfirmed": True, "litigationPending": False})
        assert objection["code"] == "0000", (s["id"], objection)
        ids[s["id"]] = objection["data"]["objectionId"]
    (HERE / "objections.json").write_text(json.dumps(ids, indent=1))
    print("registered", len(ids))


def drive(target: int):
    admin = Admin()
    ids = json.loads((HERE / "objections.json").read_text())
    while True:
        rows = sql("SELECT objection_id, COUNT(*), SUM(status IN ('COMPLETED','FAILED')), MAX(status IN ('QUEUED','RUNNING'))"
                   " FROM agent_run GROUP BY objection_id")
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
    ids = json.loads((HERE / "objections.json").read_text())
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
    FROM agent_run r ORDER BY r.run_id"""
    runs = [json.loads(line) for line in sql(query)]
    for run in runs:
        for key in ("draft", "gate", "submitted"):
            if isinstance(run[key], str):
                run[key] = json.loads(run[key])
        run["tools"] = [json.loads(t) if isinstance(t, str) else t for t in (run["tools"] or [])]
    by_objection = {v: k for k, v in ids.items()}
    for run in runs:
        run["scenario"] = by_objection.get(run["objection"])
    (HERE / "results.json").write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    print("collected", len(runs))


if __name__ == "__main__":
    {"register": lambda: register(), "drive": lambda: drive(int(sys.argv[2])), "collect": lambda: collect()}[sys.argv[1]]()
