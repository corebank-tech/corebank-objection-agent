"""서버 내부 API 클라이언트. 계약은 docs/phase2/objection_agent_contract.md."""

from __future__ import annotations

from typing import Any

import httpx


class ServerError(RuntimeError):
    def __init__(self, status: int, code: str | None, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code


class ServerClient:
    def __init__(self, base_url: str, worker_key: str, transport: httpx.BaseTransport | None = None):
        self._worker_key = worker_key
        self._http = httpx.Client(base_url=base_url, timeout=30.0, transport=transport)

    def claim(self) -> dict[str, Any] | None:
        return self._call("POST", "/internal/review-jobs/claim", headers={"X-Worker-Key": self._worker_key})

    def context(self, token: str, objection_id: int) -> dict[str, Any]:
        return self._call("GET", f"/internal/objections/{objection_id}/context", token=token)

    def ledger(
        self,
        token: str,
        objection_id: int,
        start: str | None = None,
        end: str | None = None,
        counterparty: str | None = None,
    ) -> list[dict[str, Any]]:
        params = {k: v for k, v in {"from": start, "to": end, "counterparty": counterparty}.items() if v}
        return self._call("GET", f"/internal/objections/{objection_id}/ledger", token=token, params=params)

    def record_step(self, token: str, run_id: int, step: dict[str, Any]) -> None:
        self._call("POST", f"/internal/agent-runs/{run_id}/steps", token=token, json=step)

    def submit_draft(self, token: str, objection_id: int, draft: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/internal/objections/{objection_id}/review-draft", token=token, json=draft)

    def _call(self, method: str, path: str, token: str | None = None, headers: dict | None = None, **kwargs) -> Any:
        merged = dict(headers or {})
        if token is not None:
            merged["X-Job-Token"] = token
        response = self._http.request(method, path, headers=merged, **kwargs)
        body = response.json() if response.content else {}
        if response.status_code != 200 or body.get("code") != "0000":
            raise ServerError(response.status_code, body.get("code"), body.get("message", response.text))
        return body.get("data")
