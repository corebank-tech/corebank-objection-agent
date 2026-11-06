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
    """실행 전체 마감. 작업 토큰 유효 시간(서버 10분)보다 짧게 잡아 토큰이 살아 있을 때 실패를 기록한다."""

    expires_at: float  # time.monotonic() 기준

    @staticmethod
    def after(seconds: float) -> Deadline:
        return Deadline(time.monotonic() + seconds)

    def remaining(self) -> float:
        return self.expires_at - time.monotonic()

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
