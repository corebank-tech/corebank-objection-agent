"""분석기 포트. 워커는 이 인터페이스만 알고, Claude 호출과 오프라인 규칙 분석기를 바꿔 끼운다."""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol

# 원장 조회 함수: (from, to, counterparty) -> 원장 항목 목록
LedgerFn = Callable[[str | None, str | None, str | None], list[dict[str, Any]]]

# 외부 확인 회신 조회 함수: () -> [{agency, status, reply}]. 서버가 이 API 를 모르면(옛 계약) None 을 넘긴다
ExternalChecksFn = Callable[[], list[dict[str, Any]]]

LEDGER_TOOL_NAME = "get_ledger"
EXTERNAL_CHECKS_TOOL_NAME = "get_external_checks"


@dataclass
class ToolCall:
    reason: str
    args: dict[str, Any]
    result: list[dict[str, Any]]
    tool: str = LEDGER_TOOL_NAME


# 도구를 부를 때마다 워커가 TOOL_CALL 단계를 남기도록 알린다
ToolCallback = Callable[[ToolCall], None]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: Usage) -> None:
        self.input_tokens += other.input_tokens
        self.output_tokens += other.output_tokens
        self.cache_read_tokens += other.cache_read_tokens
        self.cache_write_tokens += other.cache_write_tokens


@dataclass
class Analysis:
    draft: dict[str, Any]  # prompts.DRAFT_SCHEMA 모양
    model: str
    usage: Usage = field(default_factory=Usage)
    cost_micro_usd: int = 0


class AnalysisRefused(RuntimeError):
    """모델이 응답을 거절했거나 형식을 지키지 못했다. 워커가 FAILED 단계로 남긴다."""


class AnalysisAborted(RuntimeError):
    """작업 토큰이 만료됐거나 점유를 잃었다. 서버가 실행을 이미 정리했으므로 더 부르지 않고 바로 멈춘다."""


class DeadlineExceeded(RuntimeError):
    """실행 마감을 넘겼다. 토큰이 만료되기 전에 FAILED 로 남겨 재시도하게 한다."""


@dataclass
class Deadline:
    """실행 마감. 두 시계 중 먼저 오는 쪽을 따른다.

    - 실행 전체: 작업 토큰 유효 시간(서버 10분)보다 짧게 잡는다.
    - 점유: 서버는 단계를 기록할 때만 점유를 연장한다(서버 5분). 모델이 오래 생각하는 동안 단계가 없으면 점유가 끊겨 토큰이
      무효가 되므로, 마지막 단계 기록 시각부터 점유 시간 안에 끝나야 한다. lease_anchor 가 마지막 기록 시각(monotonic)을 준다.
    """

    expires_at: float  # time.monotonic() 기준
    lease_anchor: Callable[[], float] | None = None
    lease_seconds: float = 0.0

    @staticmethod
    def after(seconds: float, lease_anchor: Callable[[], float] | None = None, lease_seconds: float = 0.0) -> Deadline:
        return Deadline(time.monotonic() + seconds, lease_anchor, lease_seconds)

    def remaining(self) -> float:
        now = time.monotonic()
        left = self.expires_at - now
        if self.lease_anchor is not None and self.lease_seconds > 0:
            left = min(left, self.lease_anchor() + self.lease_seconds - now)
        return left

    def check(self, stage: str, reserve: float = 0.0) -> None:
        if self.remaining() <= reserve:
            raise DeadlineExceeded(f"실행 마감 초과({stage})")


class Analyzer(Protocol):
    def analyze(
        self,
        context: dict[str, Any],
        ledger: LedgerFn,
        on_tool_call: ToolCallback,
        external_checks: ExternalChecksFn | None = None,
        deadline: Deadline | None = None,
    ) -> Analysis: ...
