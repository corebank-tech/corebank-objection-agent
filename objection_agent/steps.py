"""단계 이벤트 기록. 순번은 서버가 알려 준 nextSeq 부터 빈 번호 없이 매긴다(agent_run_step_schema §3)."""

from __future__ import annotations

import time
from datetime import datetime
from typing import Any

from objection_agent.server import ServerClient


class StepRecorder:
    def __init__(self, server: ServerClient, token: str, run_id: int, next_seq: int, delay_seconds: float = 0.0):
        self._server = server
        self._token = token
        self._run_id = run_id
        self._seq = next_seq
        self._delay = delay_seconds
        # 단계의 시작은 앞 단계가 끝난 때다. 모델이 생각하고 도구를 고른 시간이 그 단계의 소요 시간으로 보인다
        self._last_end = datetime.now()

    def record(
        self,
        step_type: str,
        title: str,
        summary: str | None = None,
        detail: dict[str, Any] | None = None,
        status: str = "DONE",
        started_at: datetime | None = None,
    ) -> None:
        now = datetime.now()
        self._server.record_step(
            self._token,
            self._run_id,
            {
                "seq": self._seq,
                "type": step_type,
                "title": title[:200],
                "summary": summary[:500] if summary else None,
                "detail": detail,
                "status": status,
                "startedAt": (started_at or self._last_end).isoformat(timespec="microseconds"),
                "endedAt": now.isoformat(timespec="microseconds"),
            },
        )
        self._seq += 1
        if self._delay:
            time.sleep(self._delay)
        self._last_end = datetime.now()
