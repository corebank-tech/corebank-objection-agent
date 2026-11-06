"""Claude 분석기. 원장 조회와 외부 확인 회신 조회 도구로 직접 도구 루프를 돌고, 마지막 답은 구조화 출력(JSON 스키마)으로 받는다."""

from __future__ import annotations

import json
import time
from typing import Any

import anthropic

from objection_agent import prompts
from objection_agent.llm.port import (
    EXTERNAL_CHECKS_TOOL_NAME,
    Analysis,
    AnalysisAborted,
    AnalysisRefused,
    Deadline,
    DeadlineExceeded,
    ExternalChecksFn,
    LedgerFn,
    ToolCall,
    ToolCallback,
    Usage,
)

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

# 요청 하나의 시간 제한(초). 실행 마감이 더 가까우면 그쪽에 맞춘다
REQUEST_TIMEOUT = 120.0
# 다시 시도할 응답 코드. 429 는 분당 한도, 529 는 과부하
RETRY_STATUSES = (429, 500, 502, 503, 504, 529)
MAX_ATTEMPTS = 6
# 모델 호출 직전에 이만큼 남아 있지 않으면 부르지 않는다
CALL_RESERVE_SECONDS = 20.0
# 도구 결과 상한. 거래가 많은 계좌에서 턴마다 입력이 수십만 토큰으로 불어나는 것을 막는다
MAX_TOOL_ITEMS = 200
MAX_TOOL_CHARS = 60_000
# 한 분석에서 도구를 부르는 총횟수 상한(한 턴 안의 병렬 호출 포함)
MAX_TOOL_CALLS = 16


def _price(model: str) -> dict[str, float]:
    """응답 모델 이름(날짜 접미사가 붙을 수 있다)으로 단가를 찾는다. 모르는 모델이면 가장 비싼 Opus 단가로 센다."""
    return next((v for k, v in PRICES_MICRO_USD.items() if model.startswith(k)), PRICE_MICRO_USD)


def cost_micro_usd(usage: Usage, model: str = "claude-opus-5-5") -> int:
    price = _price(model)
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
        # 재시도는 실행 마감을 보며 직접 한다(_create). SDK 재시도는 마감을 모른다
        self._client = client or anthropic.Anthropic(max_retries=0)

    def analyze(
        self,
        context: dict[str, Any],
        ledger: LedgerFn,
        on_tool_call: ToolCallback,
        external_checks: ExternalChecksFn | None = None,
        deadline: Deadline | None = None,
    ) -> Analysis:
        messages: list[dict[str, Any]] = [
            {"role": "user", "content": [{"type": "text", "text": prompts.user_message(context)}]}
        ]
        usage = Usage()
        cost = 0.0
        tool_calls = 0
        model = self._model
        # 외부 확인 API 가 없는 옛 서버면 도구를 주지 않는다
        tools = [prompts.LEDGER_TOOL] + ([prompts.EXTERNAL_CHECKS_TOOL] if external_checks is not None else [])

        fallback = (
            {} if self._model.startswith(NO_FALLBACK_PREFIXES) else {"betas": [FALLBACK_BETA], "fallbacks": "default"}
        )
        for _ in range(self._max_tool_turns + 1):
            _mark_cache(messages)
            response = self._create(
                deadline,
                model=self._model,
                max_tokens=16000,
                system=[{"type": "text", "text": prompts.SYSTEM_PROMPT, "cache_control": {"type": "ephemeral"}}],
                tools=tools,
                output_config={
                    "effort": self._effort,
                    "format": {"type": "json_schema", "schema": prompts.DRAFT_SCHEMA},
                },
                messages=messages,
                **fallback,
            )
            turn = _usage(response.usage)
            usage.add(turn)
            model = response.model or model
            # 서버 쪽 fallback 으로 다른 모델이 답했으면 그 모델 단가로 센다
            cost += cost_micro_usd(turn, model)

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
                    tool_calls += 1
                    if tool_calls > MAX_TOOL_CALLS:
                        results.append(
                            _tool_error(
                                block, f"도구 호출 한도 {MAX_TOOL_CALLS}회를 넘음. 지금까지 본 원장으로 판정안을 내세요"
                            )
                        )
                        continue
                    results.append(_run_tool(block, ledger, external_checks, on_tool_call))
                messages.append({"role": "user", "content": results})
                continue

            text = "".join(block.text for block in response.content if block.type == "text")
            try:
                draft = json.loads(text)
            except json.JSONDecodeError as exc:
                raise AnalysisRefused("판정안 형식이 맞지 않음") from exc
            return Analysis(draft=draft, model=model, usage=usage, cost_micro_usd=round(cost))

        raise AnalysisRefused(f"도구 호출이 {self._max_tool_turns}회를 넘음")

    def _create(self, deadline: Deadline | None, **kwargs: Any) -> Any:
        """요청 하나. 일시 오류는 실행 마감 안에서만 다시 시도하고, 요청 시간 제한도 마감에 맞춘다."""
        for attempt in range(1, MAX_ATTEMPTS + 1):
            timeout = REQUEST_TIMEOUT
            if deadline is not None:
                deadline.check("모델 호출", CALL_RESERVE_SECONDS)
                timeout = min(timeout, deadline.remaining() - CALL_RESERVE_SECONDS / 2)
            try:
                return self._client.beta.messages.create(timeout=timeout, **kwargs)
            except anthropic.APIStatusError as exc:
                if exc.status_code not in RETRY_STATUSES or attempt == MAX_ATTEMPTS:
                    raise
                wait = _retry_after(exc) or min(2.0**attempt, 30.0)
            except anthropic.APIConnectionError:  # 시간 초과 포함
                if attempt == MAX_ATTEMPTS:
                    raise
                wait = min(2.0**attempt, 30.0)
            if deadline is not None and deadline.remaining() - wait <= CALL_RESERVE_SECONDS:
                raise DeadlineExceeded("실행 마감 안에 모델 응답을 받지 못함")
            time.sleep(wait)
        raise AssertionError("도달하지 않는다")  # pragma: no cover


def _retry_after(exc: anthropic.APIStatusError) -> float | None:
    try:
        return float(exc.response.headers.get("retry-after", ""))
    except (TypeError, ValueError, AttributeError):
        return None


def _mark_cache(messages: list[dict[str, Any]]) -> None:
    """마지막 사용자 메시지의 끝 블록에만 캐시 지점을 둔다. 앞 턴까지의 이력은 다음 턴에 캐시 읽기 단가로 센다.

    캐시 지점은 요청당 4개까지라 앞 턴에 둔 지점은 지운다. 모델 응답(assistant)은 손대지 않는다.
    """
    for message in messages:
        if message["role"] == "user" and isinstance(message["content"], list):
            for block in message["content"]:
                if isinstance(block, dict):
                    block.pop("cache_control", None)
    last = messages[-1]["content"][-1]
    if isinstance(last, dict):
        last["cache_control"] = {"type": "ephemeral"}


def _tool_error(block: Any, message: str) -> dict[str, Any]:
    return {"type": "tool_result", "tool_use_id": block.id, "content": message, "is_error": True}


def _tool_content(items: list[dict[str, Any]]) -> str:
    """도구 결과를 JSON 으로 넣되 건수와 글자 수 상한 안에서만 넣고, 잘랐으면 그 사실을 모델에 알린다."""
    kept = items[:MAX_TOOL_ITEMS]
    text = json.dumps(kept, ensure_ascii=False)
    while len(text) > MAX_TOOL_CHARS and kept:
        kept = kept[: len(kept) * 3 // 4]
        text = json.dumps(kept, ensure_ascii=False)
    if len(kept) < len(items):
        text += f"\n(전체 {len(items)}건 중 앞 {len(kept)}건만 보냄. 기간이나 상대방으로 좁혀 다시 조회하세요)"
    return text


def _run_tool(
    block: Any, ledger: LedgerFn, external_checks: ExternalChecksFn | None, on_tool_call: ToolCallback
) -> dict[str, Any]:
    args = dict(block.input)
    reason = str(args.pop("reason", ""))
    try:
        if block.name == EXTERNAL_CHECKS_TOOL_NAME:
            if external_checks is None:
                raise ValueError("이 서버는 외부 확인 조회를 지원하지 않음")
            items = external_checks()
        else:
            items = ledger(args.get("from"), args.get("to"), args.get("counterparty"))
    except (AnalysisAborted, DeadlineExceeded):  # 토큰 만료나 마감 초과는 모델에 돌려주지 않고 실행을 멈춘다
        raise
    except Exception as exc:  # 조회 실패는 모델에 알려 다른 조회를 고르게 한다(허용 기간 안내 등)
        return _tool_error(block, f"조회 실패: {exc}"[:300])
    on_tool_call(ToolCall(reason=reason, args={k: v for k, v in args.items() if v}, result=items, tool=block.name))
    return {"type": "tool_result", "tool_use_id": block.id, "content": _tool_content(items)}


def _usage(raw: Any) -> Usage:
    return Usage(
        input_tokens=raw.input_tokens or 0,
        output_tokens=raw.output_tokens or 0,
        cache_read_tokens=getattr(raw, "cache_read_input_tokens", None) or 0,
        cache_write_tokens=getattr(raw, "cache_creation_input_tokens", None) or 0,
    )
