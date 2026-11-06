"""내부 API 계약(objection_agent_contract.md)을 흉내 내는 가짜 서버. httpx.MockTransport 로 붙인다."""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

DISPUTED = "20991231WB0000000009"


def item(
    number: str, direction: str, amount: int, at: str, cp: str | None = "CP-0000000a", disputed=False, after=False
):
    return {
        "transactionNumber": number,
        "direction": direction,
        "interbank": cp is None,
        "counterpartyKey": cp,
        "amount": amount,
        "occurredAt": at,
        "disputed": disputed,
        "afterDispute": after,
    }


def relationship_ledger() -> list[dict[str, Any]]:
    return [
        item("20991231WB0000000001", "IN", 100_000, "2026-09-26T10:00:00"),
        item("20991231WB0000000002", "OUT", 100_000, "2026-10-16T10:00:00"),
        item("20991231WB0000000003", "IN", 100_000, "2026-10-31T10:00:00"),
        item(DISPUTED, "IN", 300_000, "2026-11-05T10:00:00", disputed=True),
    ]


def mule_ledger() -> list[dict[str, Any]]:
    return [
        item(DISPUTED, "IN", 300_000, "2026-11-05T10:00:00", disputed=True),
        item("20991231WB0000000011", "IN", 500_000, "2026-11-05T10:05:00", cp=None, after=True),
        item("20991231WB0000000012", "IN", 400_000, "2026-11-05T10:10:00", cp="CP-0000000b", after=True),
        item("20991231WB0000000013", "OUT", 1_150_000, "2026-11-05T10:30:00", cp=None, after=True),
    ]


def document(
    document_id: int, content: bytes = b"\x89PNG\r\n\x1a\n", kind="EVIDENCE", ocr_status="PENDING", pages=None
):
    return {
        "documentId": document_id,
        "kind": kind,
        "contentType": "image/png",
        "round": 0,
        "ocrStatus": ocr_status,
        "content": content,
        "pages": pages,
    }


class FakeServer:
    """documents 나 external_checks 를 주면 contract-v2 서버, 둘 다 None 이면 옛 서버처럼 응답한다."""

    def __init__(
        self,
        ledger: list[dict[str, Any]],
        statement: str,
        fail_draft_code: str | None = None,
        documents: list[dict[str, Any]] | None = None,
        external_checks: list[dict[str, Any]] | None = None,
        leak_id_copy: bool = False,
        gate_branch: str | None = None,
    ):
        self.ledger = ledger
        self.statement = statement
        self.fail_draft_code = fail_draft_code
        self.v2 = documents is not None or external_checks is not None
        self.documents = documents or []
        self.external_checks = external_checks or []
        # 서버가 계약을 어기고 신분증 사본을 내부 목록에 섞어 주는 경우를 흉내 낸다. 워커는 그래도 내려받지 않아야 한다
        self.leak_id_copy = leak_id_copy
        # None 이면 해제 요건 점검 API 가 없는 옛 서버처럼 404 를 돌려준다
        self.gate_branch = gate_branch
        self.paths: list[str] = []
        self.ocr_puts: list[dict[str, Any]] = []
        self.forbidden: list[str] = []
        self.content_errors: dict[int, tuple[int, str]] = {}  # documentId → (HTTP 상태, 오류코드)
        self.queue = [{"runId": 41, "objectionId": 7, "attempt": 1, "nextSeq": 1, "jobToken": "tok"}]
        self.steps: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []
        self.ledger_calls: list[dict[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        self.paths.append(f"{request.method} {path}")
        if path == "/internal/review-jobs/claim":
            assert request.headers["X-Worker-Key"] == "key"
            return _ok(self.queue.pop(0) if self.queue else None)
        assert request.headers["X-Job-Token"] == "tok"
        if path == "/internal/objections/7/context":
            return _ok(
                {
                    "objectionId": 7,
                    "runId": 41,
                    "clause": 1,
                    "statement": self.statement,
                    "simplifiedRequested": False,
                    "idCopyConfirmed": True,
                    "litigationPending": False,
                    "objectionDeadline": "2026-12-05",
                    "disputedTransfers": [i for i in self.ledger if i["disputed"]],
                    "commonDocuments": ["거래 사실 확인 자료", "상대방과의 관계 소명 자료"],
                    "ledgerFrom": "2026-05-09T10:00:00",
                    "ledgerTo": "2026-11-06T10:00:00",
                    **(self._context_v2() if self.v2 else {}),
                }
            )
        if self.v2 and path.startswith("/internal/objections/7/"):
            handled = self._handle_v2(request, path.removeprefix("/internal/objections/7/"))
            if handled is not None:
                return handled
        if path == "/internal/objections/7/gate" and self.gate_branch is not None:
            return _ok({"branch": self.gate_branch})
        if path == "/internal/objections/7/ledger":
            params = dict(request.url.params)
            self.ledger_calls.append(params)
            cp = params.get("counterparty")
            return _ok([i for i in self.ledger if cp is None or i["counterpartyKey"] == cp])
        if path == "/internal/agent-runs/41/steps":
            step = json.loads(request.content)
            assert step["seq"] == len(self.steps) + 1
            self.steps.append(step)
            return _ok(None)
        if path == "/internal/objections/7/review-draft":
            if self.fail_draft_code:
                return httpx.Response(400, json={"code": self.fail_draft_code, "message": "거부"})
            draft = json.loads(request.content)
            self.drafts.append(draft)
            return _ok({"runId": 41, "agentBranch": draft["branch"], "gateBranch": draft["branch"]})
        return httpx.Response(404, json={"code": "CMN0404", "message": path})

    def _visible(self) -> list[dict[str, Any]]:
        return [d for d in self.documents if d["kind"] == "EVIDENCE" or self.leak_id_copy]

    def _context_v2(self) -> dict[str, Any]:
        return {
            "documents": [
                {
                    "documentId": d["documentId"],
                    "round": d["round"],
                    "ocrStatus": d["ocrStatus"],
                    "pages": d["pages"] if d["ocrStatus"] == "DONE" else None,
                    **({"kind": d["kind"]} if self.leak_id_copy else {}),
                }
                for d in self._visible()
            ],
            "externalChecks": self.external_checks,
            "supplementRound": 0,
            "missingDocumentsRequested": [],
        }

    def _handle_v2(self, request: httpx.Request, rest: str) -> httpx.Response | None:
        if rest == "documents":
            return _ok(
                [
                    {
                        "documentId": d["documentId"],
                        "contentType": d["contentType"],
                        "round": d["round"],
                        "ocrStatus": d["ocrStatus"],
                        **({"kind": d["kind"]} if self.leak_id_copy else {}),
                    }
                    for d in self._visible()
                ]
            )
        if rest == "external-checks":
            return _ok([{k: c[k] for k in ("agency", "status", "reply")} for c in self.external_checks])
        match = re.fullmatch(r"documents/(\d+)/(content|ocr)", rest)
        if match is None:
            return None
        doc = next((d for d in self.documents if d["documentId"] == int(match.group(1))), None)
        if doc is None or doc["kind"] != "EVIDENCE":
            # 신분증 사본이나 없는 문서를 건드리면 계약 위반이다. 테스트가 forbidden 으로 잡는다
            self.forbidden.append(f"{request.method} {rest}")
            return httpx.Response(404, json={"code": "PST0205", "message": "문서 없음"})
        if match.group(2) == "content" and doc["documentId"] in self.content_errors:
            status, code = self.content_errors[doc["documentId"]]
            return httpx.Response(status, json={"code": code, "message": "오류"})
        if match.group(2) == "content":
            return httpx.Response(200, content=doc["content"], headers={"Content-Type": doc["contentType"]})
        body = json.loads(request.content)
        self.ocr_puts.append({"documentId": doc["documentId"], **body})
        pages = _pseudonymize(body["pages"])
        doc["pages"], doc["ocrStatus"] = pages, "DONE"
        return _ok({"documentId": doc["documentId"], "pages": pages})


def _pseudonymize(pages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """서버 가명 처리 흉내: 휴대전화 번호만 자리표시로 바꾼다."""
    return [
        {
            **page,
            "regions": [{**r, "text": re.sub(r"01\d-\d{3,4}-\d{4}", "[연락처]", r["text"])} for r in page["regions"]],
        }
        for page in pages
    ]


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": "0000", "message": "성공", "data": data})
