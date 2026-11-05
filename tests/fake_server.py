"""내부 API 계약(objection_agent_contract.md)을 흉내 내는 가짜 서버. httpx.MockTransport 로 붙인다."""

from __future__ import annotations

import json
from typing import Any

import httpx

DISPUTED = "20991231WB0000000009"


def item(number: str, direction: str, amount: int, at: str, cp: str | None = "CP-0000000a", disputed=False, after=False):
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


class FakeServer:
    def __init__(self, ledger: list[dict[str, Any]], statement: str, fail_draft_code: str | None = None):
        self.ledger = ledger
        self.statement = statement
        self.fail_draft_code = fail_draft_code
        self.queue = [{"runId": 41, "objectionId": 7, "attempt": 1, "nextSeq": 1, "jobToken": "tok"}]
        self.steps: list[dict[str, Any]] = []
        self.drafts: list[dict[str, Any]] = []
        self.ledger_calls: list[dict[str, str]] = []

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path.removeprefix("/api/v1")
        if path == "/internal/review-jobs/claim":
            assert request.headers["X-Worker-Key"] == "key"
            return _ok(self.queue.pop(0) if self.queue else None)
        assert request.headers["X-Job-Token"] == "tok"
        if path == "/internal/objections/7/context":
            return _ok({
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
            })
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


def _ok(data: Any) -> httpx.Response:
    return httpx.Response(200, json={"code": "0000", "message": "성공", "data": data})
