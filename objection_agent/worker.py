"""워커 루프. 대기열에서 분석 하나를 가져가 단계 이벤트를 남기며 판정안을 낸다(agent_run_step_schema §2)."""

from __future__ import annotations

import logging
import time
from typing import Any

from objection_agent import prompts
from objection_agent.config import Config
from objection_agent.llm.port import Analysis, Analyzer, ToolCall
from objection_agent.server import ServerClient, ServerError
from objection_agent.steps import StepRecorder

log = logging.getLogger("objection_agent")


def run_once(server: ServerClient, analyzer: Analyzer, step_delay: float = 0.0) -> bool:
    """분석 하나를 처리한다. 대기열이 비었으면 False."""
    job = server.claim()
    if job is None:
        return False
    token, run_id, objection_id = job["jobToken"], job["runId"], job["objectionId"]
    steps = StepRecorder(server, token, run_id, job["nextSeq"], step_delay)
    log.info("분석 시작 runId=%s objectionId=%s attempt=%s", run_id, objection_id, job["attempt"])

    try:
        steps.record("CLAIMED", "분석 작업 시작", f"{job['attempt']}번째 시도", {"attempt": job["attempt"]})
        context = server.context(token, objection_id)
        steps.record(
            "CONTEXT_LOADED",
            "소명과 다툼 거래 확인",
            f"다툼 거래 {len(context['disputedTransfers'])}건, 원장 조회 기간 {context['ledgerFrom'][:10]}~{context['ledgerTo'][:10]}",
            {
                "clause": context["clause"],
                "disputedTransactionNumbers": [t["transactionNumber"] for t in context["disputedTransfers"]],
                "ledgerFrom": context["ledgerFrom"],
                "ledgerTo": context["ledgerTo"],
                "simplifiedRequested": context["simplifiedRequested"],
                "litigationPending": context["litigationPending"],
            },
        )

        # 판정안 근거는 Agent가 실제로 본 거래번호로만 한정한다. 만들어 낸 번호는 서버가 PST0005로 막기 전에 걸러 낸다
        # 정상 갈래 근거에서 빼야 하는 다툼 거래와 다툼 이후 거래도 함께 기억한다
        seen = {t["transactionNumber"] for t in context["disputedTransfers"]}
        tainted = set(seen)

        def ledger(start: str | None, end: str | None, counterparty: str | None) -> list[dict[str, Any]]:
            items = server.ledger(token, objection_id, start, end, counterparty)
            seen.update(i["transactionNumber"] for i in items)
            tainted.update(i["transactionNumber"] for i in items if i["disputed"] or i["afterDispute"])
            return items

        def on_tool_call(call: ToolCall) -> None:
            steps.record(
                "TOOL_CALL",
                "원장 조회",
                call.reason,
                {
                    "tool": "get_ledger",
                    "reason": call.reason,
                    "args": call.args,
                    "resultSummary": _ledger_summary(call.result),
                    "transactionNumbers": [i["transactionNumber"] for i in call.result][:50],
                },
            )

        analysis = analyzer.analyze(context, ledger, on_tool_call)
        dropped = _keep_seen(analysis.draft, seen, tainted)
        _record_findings(steps, analysis, dropped)
        result = server.submit_draft(token, objection_id, _draft_request(analysis))
        log.info("판정안 제출 runId=%s agent=%s gate=%s", run_id, result["agentBranch"], result["gateBranch"])
    except ServerError as exc:
        # 토큰 만료나 점유 상실이면 서버가 이미 실행을 정리했다. 단계를 더 남기지 않는다
        log.warning("서버 오류 runId=%s %s", run_id, exc)
        if exc.code not in ("PST0102",):
            _fail(steps, f"서버 응답 오류 {exc.code}")
    except Exception as exc:  # 분석 실패는 FAILED 단계로 남겨 재시도하게 한다
        log.exception("분석 실패 runId=%s", run_id)
        _fail(steps, str(exc)[:200] or exc.__class__.__name__)
    return True


def _keep_seen(draft: dict[str, Any], seen: set[str], tainted: set[str]) -> dict[str, list[str]]:
    """걸러 낸 번호를 돌려준다. unseen 은 조회하지 않은 번호(지어냈을 가능성), tainted 는 정상 근거로 쓸 수 없는 번호다."""
    excluded = tainted if draft["branch"] in ("NORMAL_ACCEPT", "NORMAL_PARTIAL") else set()
    referenced = list(draft["evidenceTransactionNumbers"])
    for item in draft["contradictions"]:
        referenced += item["transactionNumbers"]
    dropped = {
        "unseen": sorted({n for n in referenced if n not in seen}),
        "tainted": sorted({n for n in draft["evidenceTransactionNumbers"] if n in seen and n in excluded}),
    }
    draft["evidenceTransactionNumbers"] = [
        n for n in draft["evidenceTransactionNumbers"] if n in seen and n not in excluded
    ]
    for item in draft["contradictions"]:
        item["transactionNumbers"] = [n for n in item["transactionNumbers"] if n in seen]
    return dropped


def _record_findings(steps: StepRecorder, analysis: Analysis, dropped: dict[str, list[str]]) -> None:
    draft = analysis.draft
    if draft["claims"]:
        steps.record(
            "CLAIM_EXTRACTED",
            "소명 주장 추출",
            f"주장 {len(draft['claims'])}건",
            {"claims": draft["claims"]},
        )
    for item in draft["contradictions"]:
        steps.record("CONTRADICTION", "주장과 원장이 어긋남", item["ledgerFact"], item)
    steps.record(
        "DRAFT_SUBMITTED",
        "판정안 제출",
        draft["branchReason"],
        {
            "branch": draft["branch"],
            "evidenceTransactionNumbers": draft["evidenceTransactionNumbers"],
            "missingDocuments": draft["missingDocuments"],
            "model": analysis.model,
            "costMicroUsd": analysis.cost_micro_usd,
            "droppedTransactionNumbers": dropped,
        },
    )


def _draft_request(analysis: Analysis) -> dict[str, Any]:
    draft = analysis.draft
    return {
        "branch": draft["branch"],
        "evidenceTransactionNumbers": draft["evidenceTransactionNumbers"],
        "missingDocuments": draft["missingDocuments"],
        "claims": draft["claims"],
        "contradictions": draft["contradictions"],
        "followUps": draft["followUps"],
        "noticeDraft": draft["noticeDraft"],
        "model": analysis.model[:60],
        "promptVersion": prompts.PROMPT_VERSION,
        "inputTokens": analysis.usage.input_tokens + analysis.usage.cache_read_tokens + analysis.usage.cache_write_tokens,
        "outputTokens": analysis.usage.output_tokens,
        "costMicroUsd": analysis.cost_micro_usd,
    }


def _fail(steps: StepRecorder, reason: str) -> None:
    try:
        steps.record("FAILED", "분석 실패", reason, {"reason": reason}, status="FAILED")
    except ServerError as exc:
        log.warning("실패 단계 기록 실패 %s", exc)


def _ledger_summary(items: list[dict[str, Any]]) -> str:
    incoming = [i for i in items if i["direction"] == "IN"]
    outgoing = [i for i in items if i["direction"] == "OUT"]
    return (
        f"{len(items)}건 (입금 {len(incoming)}건 {sum(i['amount'] for i in incoming):,}원, "
        f"출금 {len(outgoing)}건 {sum(i['amount'] for i in outgoing):,}원)"
    )


def build_analyzer(config: Config) -> Analyzer:
    if config.llm == "claude":
        from objection_agent.llm.claude import ClaudeAnalyzer

        return ClaudeAnalyzer(config.model, config.effort, config.max_tool_turns)
    from objection_agent.llm.scripted import ScriptedAnalyzer

    return ScriptedAnalyzer()


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    config = Config.from_env()
    if not config.worker_key:
        raise SystemExit("PAYMENT_STOP_WORKER_KEY 가 비어 있습니다.")
    server = ServerClient(config.server_base_url, config.worker_key)
    analyzer = build_analyzer(config)
    log.info("워커 시작 server=%s llm=%s model=%s", config.server_base_url, config.llm, config.model)
    while True:
        try:
            if not run_once(server, analyzer, config.step_delay_seconds):
                time.sleep(config.poll_seconds)
        except KeyboardInterrupt:
            break
        except Exception:  # 서버가 잠깐 내려가도 워커는 계속 돈다
            log.exception("작업 가져가기 실패")
            time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
