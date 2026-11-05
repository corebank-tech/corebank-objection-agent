"""Claude 분석기. 원장 조회 도구 하나로 직접 도구 루프를 돌고, 마지막 답은 구조화 출력(JSON 스키마)으로 받는다."""

from __future__ import annotations

import json
from typing import Any

import anthropic

from objection_agent import prompts
from objection_agent.llm.port import Analysis, AnalysisRefused, LedgerFn, ToolCall, ToolCallback, Usage

# 백만 토큰당 달러 = 토큰당 백만분의 1달러(입력, 출력, 캐시 읽기, 5분 캐시 쓰기). Haiku 는 프롬프트 100K 이하 단가
PRICES_MICRO_USD = {
    "claude-opus-5-5": {"input": 4.0, "output": 20.0, "cache_read": 0.2, "cache_write": 5.0},
    "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "cache_read": 0.2, "cache_write": 2.5},
    "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "cache_read": 0.01, "cache_write": 0.125},
}
PRICE_MICRO_USD = PRICES_MICRO_USD["claude-opus-5-5"]

# 서버 쪽 fallback 이 없는 모델(Haiku 5.5)에는 보내지 않는다
NO_FALLBACK_PREFIXES = ("claude-haiku",)

# 안전 분류기가 거절하면 서버가 권장 모델로 다시 돌린다
FALLBACK_BETA = "server-side-fallback-2026-07-01"


def cost_micro_usd(usage: Usage, model: str = "claude-opus-5-5") -> int:
    price = PRICES_MICRO_USD.get(model, PRICE_MICRO_USD)
    return round(
        usage.input_tokens * price["input"]
        + usage.output_tokens * price["output"]
        + usage.cache_read_tokens * price["cache_read"]
        + usage.cache_write_tokens * price["cache_write"]
    )


class ClaudeAnalyzer:
    def __init__(self, model: str, effort: str, max_tool_turns: int, client: Any | None = None):
        self._model = model
        self._effort = effort
        self._max_tool_turns = max_tool_turns
        # 새 계정은 분당 한도가 낮아 429 가 잦다. SDK 재시도 간격(지수 백오프)에 맡긴다
        self._client = client or anthropic.Anthropic(max_retries=6)

    def analyze(self, context: dict[str, Any], ledger: LedgerFn, on_tool_call: ToolCallback) -> Analysis:
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompts.user_message(context)}]
        usage = Usage()
        model = self._model

        fallback = {} if self._model.startswith(NO_FALLBACK_PREFIXES) else {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        for _ in range(self._max_tool_turns + 1):
            response = self._client.beta.messages.create(
                model=self._model,
                max_tokens=16000,
                system=[{"type": "text", "text": prompts.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=[prompts.LEDGER_TOOL],
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": prompts.DRAFT_SCHEMA},
                },
                messages=messages,
                **fallback,
            )
            usage.add(_usage(response.usage))
            model = response.model or model

            if response.stop_reason == "refusal":
                raise AnalysisRefused("모델이 분석을 거절함")
            if response.stop_reason == "max_tokens":
                raise AnalysisRefused("출력 한도를 넘음")

            # 사고 블록을 포함해 응답을 바꾸지 않고 그대로 돌려준다(preserved thinking)
            messages.append({"role": "assistant", "content": response.content})

            if response.stop_reason == "tool_use":
                results = []
                for block in response.content:
                    if block.type != "tool_use":
                        continue
                    results.append(_run_tool(block, ledger, on_tool_call))
                messages.append({"role": "user", "content": results})
                continue

            text = "".join(block.text for block in response.content if block.type == "text")
            try:
                draft = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AnalysisRefused("판정안 형식이 맞지 않음") from exc
            return Analysis(draft=draft, model=model, usage=usage, cost_micro_usd=cost_micro_usd(usage, self._model))

        raise AnalysisRefused(f"도구 호출이 {self._max_tool_turns}회를 넘음")


def _run_tool(block: Any, ledger: LedgerFn, on_tool_call: ToolCallback) -> dict[str, Any]:
    args = dict(block.input)
    reason = str(args.pop("reason", ""))
    try:
        items = ledger(args.get("from"), args.get("to"), args.get("counterparty"))
    except Exception as exc:  # 조회 실패는 모델에 알려 다른 조회를 고르게 한다
        return {"type": "tool_result", "tool_use_id": block.id, "content": f"조회 실패: {exc}", "is_error": True}
    on_tool_call(ToolCall(reason=reason, args={k: v for k, v in args.items() if v}, result=items))
    return {
        "type": "tool_result",
        "tool_use_id": block.id,
        "content": json.dumps(items, ensure_ascii=False),
    }


def _usage(raw: Any) -> Usage:
    return Usage(
        input_tokens=raw.input_tokens or 0,
        output_tokens=raw.output_tokens or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", None) or 0,
    )
