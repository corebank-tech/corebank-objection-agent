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

    # 아래 네 API 는 contract-v2(서류, OCR, 외부 확인)에서 추가됐다. 옛 서버는 context 에 documents 키가 없으므로 워커가 부르지 않는다
    def documents(self, token: str, objection_id: int) -> list[dict[str, Any]]:
        """증빙(EVIDENCE) 목록. 신분증 사본은 서버가 주지 않는다(D37)."""
        return self._call("GET", f"/internal/objections/{objection_id}/documents", token=token) or []

    def document_content(self, token: str, objection_id: int, document_id: int) -> bytes:
        """파일 바이트. 이 API 만 ApiResponse 가 아니라 원본을 준다."""
        response = self._http.get(
            f"/internal/objections/{objection_id}/documents/{document_id}/content", headers={"X-Job-Token": token}
        )
        if response.status_code != 200:
            body = _json_or_empty(response)
            raise ServerError(response.status_code, body.get("code"), body.get("message", "서류 내려받기 실패"))
        return response.content

    def put_ocr(
        self, token: str, objection_id: int, document_id: int, engine: str, pages: list[dict[str, Any]]
    ) -> dict[str, Any]:
        """OCR 원문을 올린다. 서버가 가명 처리해 저장하고 가명 처리한 값을 돌려준다."""
        return self._call(
            "PUT",
            f"/internal/objections/{objection_id}/documents/{document_id}/ocr",
            token=token,
            json={"engine": engine, "pages": pages},
        )

    def external_checks(self, token: str, objection_id: int) -> list[dict[str, Any]]:
        return self._call("GET", f"/internal/objections/{objection_id}/external-checks", token=token) or []

    def record_step(self, token: str, run_id: int, step: dict[str, Any]) -> None:
        self._call("POST", f"/internal/agent-runs/{run_id}/steps", token=token, json=step)

    def submit_draft(self, token: str, objection_id: int, draft: dict[str, Any]) -> dict[str, Any]:
        return self._call("PUT", f"/internal/objections/{objection_id}/review-draft", token=token, json=draft)

    def _call(self, method: str, path: str, token: str | None = None, headers: dict | None = None, **kwargs) -> Any:
        merged = dict(headers or {})
        if token is not None:
            merged["X-Job-Token"] = token
        response = self._http.request(method, path, headers=merged, **kwargs)
        body = _json_or_empty(response)
        if response.status_code != 200 or body.get("code") != "0000":
            raise ServerError(response.status_code, body.get("code"), body.get("message", response.text))
        return body.get("data")


def _json_or_empty(response: httpx.Response) -> dict[str, Any]:
    if not response.content:
        return {}
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}
