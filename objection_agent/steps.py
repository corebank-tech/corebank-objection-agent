"""단계 이벤트 기록. 순번은 서버가 알려 준 nextSeq 부터 빈 번호 없이 매긴다(agent_run_step_schema §3)."""

from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from objection_agent.server import ServerClient

# 응답이 유실되면 같은 순번, 같은 내용으로 다시 보낸다. 서버는 같은 내용의 재전송을 받아 주고,
# 다른 내용이 같은 순번에 오면 PST0307 로 거부한다(agent_run_step_schema §3)
RETRIES = 2

# 서버 시각은 KST 로컬 시각이다. 워커가 도는 곳(컨테이너는 UTC)과 무관하게 KST 로 찍는다
KST = timezone(timedelta(hours=9))


def now_kst() -> datetime:
    return datetime.now(KST).replace(tzinfo=None)


class StepRecorder:
    def __init__(self, server: ServerClient, token: str, run_id: int, next_seq: int, delay_seconds: float = 0.0):
        self._server = server
        self._token = token
        self._run_id = run_id
        self._seq = next_seq
        self._delay = delay_seconds
        # 단계의 시작은 앞 단계가 끝난 때다. 모델이 생각하고 도구를 고른 시간이 그 단계의 소요 시간으로 보인다
        self._last_end = now_kst()

    def record(
        self,
        step_type: str,
        title: str,
        summary: str | None = None,
        detail: dict[str, Any] | None = None,
        status: str = "DONE",
        started_at: datetime | None = None,
    ) -> None:
        now = now_kst()
        step = {
            "seq": self._seq,
            "type": step_type,
            "title": title[:200],
            "summary": summary[:500] if summary else None,
            "detail": detail,
            "status": status,
            "startedAt": (started_at or self._last_end).isoformat(timespec="microseconds"),
            "endedAt": now.isoformat(timespec="microseconds"),
        }
        for attempt in range(RETRIES + 1):
            try:
                self._server.record_step(self._token, self._run_id, step)
                break
            except httpx.TransportError:
                if attempt == RETRIES:
                    raise
                time.sleep(0.5 * (attempt + 1))
        self._seq += 1
        if self._delay:
            time.sleep(self._delay)
        self._last_end = now_kst()
