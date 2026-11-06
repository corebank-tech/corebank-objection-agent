"""워커 루프. 대기열에서 분석 하나를 가져가 단계 이벤트를 남기며 판정안을 낸다(agent_run_step_schema §2)."""

from __future__ import annotations

import logging
import time
from typing import Any

from objection_agent import prompts
from objection_agent.config import Config
from objection_agent.llm.port import EXTERNAL_CHECKS_TOOL_NAME, Analysis, Analyzer, ExternalChecksFn, ToolCall
from objection_agent.ocr import OcrEngine, OcrError, TesseractEngine, count_low_confidence, count_regions
from objection_agent.policy import apply_guards
from objection_agent.server import ServerClient, ServerError
from objection_agent.steps import StepRecorder

log = logging.getLogger("objection_agent")


def run_once(server: ServerClient, analyzer: Analyzer, step_delay: float = 0.0, ocr: OcrEngine | None = None) -> bool:
    """분석 하나를 처리한다. 대기열이 비었으면 False. ocr 이 None 이면 서류 글자 인식을 건너뛴다."""
    job = server.claim()
    if job is None:
        return False
    token, run_id, objection_id = job["jobToken"], job["runId"], job["objectionId"]
    steps = StepRecorder(server, token, run_id, job["nextSeq"], step_delay)
    log.info("분석 시작 runId=%s objectionId=%s attempt=%s", run_id, objection_id, job["attempt"])

    try:
        steps.record("CLAIMED", "분석 작업 시작", f"{job['attempt']}번째 시도", {"attempt": job["attempt"]})
        context = _evidence_only(server.context(token, objection_id))
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
                **_context_v2_summary(context),
            },
        )

        # contract-v2 서버만 documents 를 준다. 옛 서버면 서류 단계 없이 지금처럼 분석한다
        if ocr is not None and "documents" in context and _read_documents(server, token, objection_id, ocr, steps):
            context = _evidence_only(server.context(token, objection_id))  # 서버가 가명 처리한 OCR 글자를 다시 받는다

        # 판정안 근거는 Agent가 실제로 본 거래번호로만 한정한다. 만들어 낸 번호는 서버가 PST0005로 막기 전에 걸러 낸다
        # 정상 갈래 근거에서 빼야 하는 다툼 거래와 다툼 이후 거래도 함께 기억한다
        seen = {t["transactionNumber"] for t in context["disputedTransfers"]}
        tainted = set(seen)

        def ledger(start: str | None, end: str | None, counterparty: str | None) -> list[dict[str, Any]]:
            items = server.ledger(token, objection_id, start, end, counterparty)
            seen.update(i["transactionNumber"] for i in items)
            tainted.update(i["transactionNumber"] for i in items if i["disputed"] or i["afterDispute"])
            return items

        external_checks: ExternalChecksFn | None = None
        if "externalChecks" in context:

            def external_checks() -> list[dict[str, Any]]:
                return server.external_checks(token, objection_id)

        def on_tool_call(call: ToolCall) -> None:
            if call.tool == EXTERNAL_CHECKS_TOOL_NAME:
                steps.record(
                    "TOOL_CALL",
                    "외부 확인 회신 조회",
                    call.reason,
                    {
                        "tool": call.tool,
                        "reason": call.reason,
                        "args": call.args,
                        "resultSummary": _checks_summary(call.result),
                        "transactionNumbers": [],
                    },
                )
                return
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

        analysis = analyzer.analyze(context, ledger, on_tool_call, external_checks)
        dropped = _keep_seen(analysis.draft, seen, tainted)
        _check_claim_sources(analysis.draft, context.get("documents"))
        adjusted = apply_guards(analysis.draft, server.gate_branch(token, objection_id))
        _record_findings(steps, analysis, dropped, adjusted)
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


def _evidence_only(context: dict[str, Any]) -> dict[str, Any]:
    """서버가 증빙만 주는 것이 계약이지만, 신분증 사본이 섞여 오더라도 분석 입력에 넣지 않는다(D37)."""
    if context.get("documents"):
        context["documents"] = [d for d in context["documents"] if d.get("kind", "EVIDENCE") == "EVIDENCE"]
    return context


def _context_v2_summary(context: dict[str, Any]) -> dict[str, Any]:
    """contract-v2 에서 늘어난 입력의 건수. 옛 서버면 비어 있다."""
    summary: dict[str, Any] = {}
    if "documents" in context:
        summary["documents"] = len(context["documents"] or [])
    if "externalChecks" in context:
        summary["externalChecks"] = len(context["externalChecks"] or [])
    if context.get("supplementRound") is not None:
        summary["supplementRound"] = context["supplementRound"]
    return summary


def _read_documents(server: ServerClient, token: str, objection_id: int, ocr: OcrEngine, steps: StepRecorder) -> bool:
    """글자를 아직 읽지 않은 증빙을 읽어 서버에 올린다. 하나라도 올렸으면 True.

    서류 하나가 실패하면 그 서류만 OCR 단계에 실패로 남기고 다음 서류로 넘어간다. 서버 오류(토큰 만료 등)는 위로 올린다.
    """
    uploaded = False
    for doc in server.documents(token, objection_id):
        # 내부 목록은 증빙만 준다. 서버가 실수로 다른 종류를 주더라도 신분증 사본은 내려받지 않는다(D37)
        if doc.get("kind", "EVIDENCE") != "EVIDENCE" or doc.get("ocrStatus") == "DONE":
            continue
        document_id = doc["documentId"]
        try:
            pages = ocr.recognize(server.document_content(token, objection_id, document_id))
        except ServerError as exc:
            if exc.status != 404:  # 토큰 만료 같은 서버 오류는 실행 전체의 문제라 위로 올린다
                raise
            _ocr_failed(steps, document_id, ocr, str(exc))
            continue
        except OcrError as exc:
            _ocr_failed(steps, document_id, ocr, str(exc))
            continue
        except Exception as exc:  # 예상 못 한 엔진 오류도 이 서류만 실패로 둔다. 내부 경로가 남지 않게 종류만 적는다
            log.exception("OCR 실패 documentId=%s", document_id)
            _ocr_failed(steps, document_id, ocr, exc.__class__.__name__)
            continue
        saved = server.put_ocr(token, objection_id, document_id, ocr.name, pages)
        saved_pages = (saved or {}).get("pages") or pages
        regions = count_regions(saved_pages)
        steps.record(
            "OCR",
            "서류 글자 인식",
            f"서류 {document_id}: {len(saved_pages)}쪽, 글자 영역 {regions}개",
            {
                "documentId": document_id,
                "pages": len(saved_pages),
                "regions": regions,
                "lowConfidenceRegions": count_low_confidence(saved_pages),
                "engine": ocr.name,
            },
        )
        uploaded = True
    return uploaded


def _ocr_failed(steps: StepRecorder, document_id: int, ocr: OcrEngine, reason: str) -> None:
    log.warning("OCR 실패 documentId=%s %s", document_id, reason)
    steps.record(
        "OCR",
        "서류 글자 인식 실패",
        f"서류 {document_id}: {reason}",
        {"documentId": document_id, "pages": 0, "regions": 0, "engine": ocr.name, "error": reason[:200]},
    )


def _check_claim_sources(draft: dict[str, Any], documents: list[dict[str, Any]] | None) -> None:
    """주장의 서류 출처는 분석기에 준 서류와 영역만 남긴다. 없는 서류를 가리키면 documentId, regionIndex 를 비운다."""
    regions = {
        doc["documentId"]: {r["index"] for page in doc.get("pages") or [] for r in page.get("regions") or []}
        for doc in documents or []
    }
    for claim in draft["claims"]:
        claim.setdefault("source", "STATEMENT")
        document_id = claim.get("documentId")
        if document_id not in regions:
            claim["documentId"] = None
            claim["regionIndex"] = None
        elif claim.get("regionIndex") not in regions[document_id]:
            claim["regionIndex"] = None


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


def _record_findings(
    steps: StepRecorder, analysis: Analysis, dropped: dict[str, list[str]], adjusted: list[dict[str, str]] | None = None
) -> None:
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
            "branchAdjustments": adjusted or [],
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
        "inputTokens": analysis.usage.input_tokens
        + analysis.usage.cache_read_tokens
        + analysis.usage.cache_write_tokens,
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


def _checks_summary(items: list[dict[str, Any]]) -> str:
    if not items:
        return "외부 확인 요청 없음"
    return ", ".join(f"{i.get('agency')} {i.get('reply') or i.get('status')}" for i in items)


def build_ocr(config: Config) -> OcrEngine | None:
    if config.ocr == "off":
        return None
    if not TesseractEngine.available():
        log.warning("tesseract 가 없어 서류 글자 인식이 실패로 남습니다. README 「OCR」 참고")
    return TesseractEngine(config.ocr_lang, max_pages=config.ocr_max_pages, preprocess=config.ocr_preprocess)


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
    ocr = build_ocr(config)
    log.info("워커 시작 server=%s llm=%s model=%s ocr=%s", config.server_base_url, config.llm, config.model, config.ocr)
    while True:
        try:
            if not run_once(server, analyzer, config.step_delay_seconds, ocr):
                time.sleep(config.poll_seconds)
        except KeyboardInterrupt:
            break
        except Exception:  # 서버가 잠깐 내려가도 워커는 계속 돈다
            log.exception("작업 가져가기 실패")
            time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
