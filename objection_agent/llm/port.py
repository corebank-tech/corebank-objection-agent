"""분석기 포트. 워커는 이 인터페이스만 알고, Claude 호출과 오프라인 규칙 분석기를 바꿔 끼운다."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

# 원장 조회 함수: (from, to, counterparty) -> 원장 항목 목록
LedgerFn = Callable[[str | None, str | None, str | None], list[dict[str, Any]]]


@dataclass
class ToolCall:
    reason: str
    args: dict[str, Any]
    result: list[dict[str, Any]]


# 도구를 부를 때마다 워커가 TOOL_CALL 단계를 남기도록 알린다
ToolCallback = Callable[[ToolCall], None]


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0

    def add(self, other: "Usage") -> None:
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


class Analyzer(Protocol):
    def analyze(self, context: dict[str, Any], ledger: LedgerFn, on_tool_call: ToolCallback) -> Analysis: ...
