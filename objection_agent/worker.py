"""워커 루프. 대기열에서 분석 하나를 가져가 단계 이벤트를 남기며 판정안을 낸다(agent_run_step_schema §2)."""

from __future__ import annotations

import logging
import re
import time
from typing import Any

from objection_agent import prompts
from objection_agent.config import Config
from objection_agent.llm.port import (
    EXTERNAL_CHECKS_TOOL_NAME,
    Analysis,
    AnalysisAborted,
    Analyzer,
    Deadline,
    DeadlineExceeded,
    ExternalChecksFn,
    ToolCall,
)
from objection_agent.ocr import OcrEngine, OcrError, TesseractEngine, count_low_confidence, count_regions
from objection_agent.policy import apply_guards
from objection_agent.server import ServerClient, ServerError
from objection_agent.steps import StepRecorder

log = logging.getLogger("objection_agent")

# 서버가 실행을 이미 정리한 오류(토큰 만료, 점유 상실). 단계를 더 남기지 않고 멈춘다
ABORT_CODES = ("PST0102",)
# 서버 DraftRequest 검증 상한(AgentInternalController.DraftRequest). 넘으면 400 이 나고 실행 전체가 재시도된다
LIST_LIMIT = 50
EVIDENCE_LIMIT = 100
TXNO_LIMIT = 20
MISSING_DOCUMENT_LIMIT = 200
FOLLOW_UP_LIMIT = 300
NOTICE_LIMIT = 2000


def run_once(
    server: ServerClient,
    analyzer: Analyzer,
    step_delay: float = 0.0,
    ocr: OcrEngine | None = None,
    deadline_seconds: float = 480.0,
) -> bool:
    """분석 하나를 처리한다. 대기열이 비었으면 False. ocr 이 None 이면 서류 글자 인식을 건너뛴다."""
    job = server.claim()
    if job is None:
        return False
    deadline = Deadline.after(deadline_seconds)
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
        unread: list[int] = []
        truncated: list[int] = []
        if (
            ocr is not None
            and "documents" in context
            and _read_documents(server, token, objection_id, ocr, steps, deadline, unread, truncated)
        ):
            context = _evidence_only(server.context(token, objection_id))  # 서버가 가명 처리한 OCR 글자를 다시 받는다

        # 판정안 근거는 Agent가 실제로 본 거래번호로만 한정한다. 만들어 낸 번호는 서버가 PST0005로 막기 전에 걸러 낸다
        # 정상 갈래 근거에서 빼야 하는 다툼 거래와 다툼 이후 거래도 함께 기억한다
        seen = {t["transactionNumber"] for t in context["disputedTransfers"]}
        tainted = set(seen)

        def ledger(start: str | None, end: str | None, counterparty: str | None) -> list[dict[str, Any]]:
            deadline.check("원장 조회")
            items = _abort_on_token_loss(lambda: server.ledger(token, objection_id, start, end, counterparty))
            seen.update(i["transactionNumber"] for i in items)
            tainted.update(i["transactionNumber"] for i in items if i["disputed"] or i["afterDispute"])
            return items

        external_checks: ExternalChecksFn | None = None
        if "externalChecks" in context:

            def external_checks() -> list[dict[str, Any]]:
                deadline.check("외부 확인 조회")
                return _abort_on_token_loss(lambda: server.external_checks(token, objection_id))

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

        analysis = analyzer.analyze(context, ledger, on_tool_call, external_checks, deadline=deadline)
        # 보정 규칙이 갈래를 바꾸면 근거 거래 필터의 기준도 바뀐 갈래여야 한다. 그래서 보정을 먼저 한다
        adjusted = apply_guards(analysis.draft, server.gate_branch(token, objection_id))
        dropped = _keep_seen(analysis.draft, seen, tainted)
        _check_claim_sources(analysis.draft, context.get("documents"))
        unverified = _check_quotes(analysis.draft, context)
        _add_document_follow_ups(analysis.draft, unread, truncated)
        _clamp(analysis.draft)
        _record_findings(steps, analysis, dropped, adjusted, unverified)
        result = server.submit_draft(token, objection_id, _draft_request(analysis))
        log.info("판정안 제출 runId=%s agent=%s gate=%s", run_id, result["agentBranch"], result["gateBranch"])
    except AnalysisAborted as exc:
        log.warning("실행 중단 runId=%s %s", run_id, exc)
    except ServerError as exc:
        # 토큰 만료나 점유 상실이면 서버가 이미 실행을 정리했다. 단계를 더 남기지 않는다
        log.warning("서버 오류 runId=%s %s", run_id, exc)
        if exc.code not in ABORT_CODES:
            _fail(steps, f"서버 응답 오류 {exc.code}")
    except Exception as exc:  # 분석 실패는 FAILED 단계로 남겨 재시도하게 한다
        log.exception("분석 실패 runId=%s", run_id)
        _fail(steps, str(exc)[:200] or exc.__class__.__name__)
    return True


def _abort_on_token_loss(call: Any) -> Any:
    """도구 안에서 토큰을 잃으면 모델에 조회 실패로 돌려주지 않고 실행을 멈춘다. 죽은 토큰으로 남은 턴을 쓰지 않는다."""
    try:
        return call()
    except ServerError as exc:
        if exc.code in ABORT_CODES:
            raise AnalysisAborted(str(exc)) from exc
        raise


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


def _read_documents(
    server: ServerClient,
    token: str,
    objection_id: int,
    ocr: OcrEngine,
    steps: StepRecorder,
    deadline: Deadline,
    unread: list[int],
    truncated: list[int],
) -> bool:
    """글자를 아직 읽지 않은 증빙을 읽어 서버에 올린다. 하나라도 올렸으면 True.

    서류 하나가 실패하면 서버에 FAILED 로 남기고 다음 서류로 넘어간다. FAILED 서류는 재분석 때 다시 읽지 않는다
    (같은 파일로 매번 같은 실패를 되풀이하지 않는다). 토큰 만료 같은 서버 오류는 위로 올린다.
    읽지 못한 서류는 unread, 쪽 상한 때문에 뒷쪽을 못 읽은 서류는 truncated 에 담는다.
    """
    uploaded = False
    for doc in server.documents(token, objection_id):
        # 내부 목록은 증빙만 준다. 서버가 실수로 다른 종류를 주더라도 kind 가 증빙이 아니면 내려받지 않는다(D37)
        if doc.get("kind", "EVIDENCE") != "EVIDENCE" or doc.get("ocrStatus") == "DONE":
            continue
        document_id = doc["documentId"]
        if doc.get("ocrStatus") == "FAILED":
            unread.append(document_id)
            continue
        deadline.check("서류 글자 인식")
        try:
            content = server.document_content(token, objection_id, document_id)
            pages = ocr.recognize(content, check=lambda: deadline.check("서류 글자 인식"))
        except ServerError as exc:
            if exc.status != 404:  # 토큰 만료 같은 서버 오류는 실행 전체의 문제라 위로 올린다
                raise
            _ocr_failed(server, token, objection_id, steps, document_id, ocr, str(exc), unread, report=False)
            continue
        except OcrError as exc:
            _ocr_failed(server, token, objection_id, steps, document_id, ocr, str(exc), unread)
            continue
        except DeadlineExceeded:
            raise
        except Exception as exc:  # 예상 못 한 엔진 오류도 이 서류만 실패로 둔다. 내부 경로가 남지 않게 종류만 적는다
            log.exception("OCR 실패 documentId=%s", document_id)
            _ocr_failed(server, token, objection_id, steps, document_id, ocr, exc.__class__.__name__, unread)
            continue
        cut = bool(getattr(ocr, "truncated", False))
        try:
            saved = server.put_ocr(token, objection_id, document_id, ocr.name, pages)
        except ServerError as exc:
            if exc.status != 400:  # 형식 검증 실패만 이 서류의 문제다
                raise
            _ocr_failed(
                server, token, objection_id, steps, document_id, ocr, "인식 결과가 서버 형식에 맞지 않음", unread
            )
            continue
        saved_pages = (saved or {}).get("pages") or pages
        regions = count_regions(saved_pages)
        if cut:
            truncated.append(document_id)
        steps.record(
            "OCR",
            "서류 글자 인식",
            f"서류 {document_id}: {len(saved_pages)}쪽, 글자 영역 {regions}개"
            + (" (쪽 상한으로 뒷쪽 미판독)" if cut else ""),
            {
                "documentId": document_id,
                "pages": len(saved_pages),
                "regions": regions,
                "lowConfidenceRegions": count_low_confidence(saved_pages),
                "engine": ocr.name,
                "truncated": cut,
            },
        )
        uploaded = True
    return uploaded


def _ocr_failed(
    server: ServerClient,
    token: str,
    objection_id: int,
    steps: StepRecorder,
    document_id: int,
    ocr: OcrEngine,
    reason: str,
    unread: list[int],
    report: bool = True,
) -> None:
    log.warning("OCR 실패 documentId=%s %s", document_id, reason)
    unread.append(document_id)
    if report:
        try:
            server.put_ocr(token, objection_id, document_id, ocr.name, [], status="FAILED")
        except ServerError as exc:
            if exc.code in ABORT_CODES:
                raise
            log.warning("OCR 실패 기록 실패 documentId=%s %s", document_id, exc)
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


def _normalize(text: str) -> str:
    return re.sub(r"\s+", "", prompts.sanitize(text))


def _check_quotes(draft: dict[str, Any], context: dict[str, Any]) -> list[int]:
    """주장의 인용이 소명이나 그 서류 영역 글에 실제로 있는지 본다. 공백을 뺀 부분 문자열로 비교한다.

    없으면 quoteVerified=false 를 붙여 담당자 화면이 「원문에서 찾지 못한 인용」으로 보이게 한다. 돌려주는 값은 그 주장의 순번이다.
    """
    statement = _normalize(str(context.get("statement") or ""))
    texts: dict[int, dict[int, str]] = {}
    for doc in context.get("documents") or []:
        texts[doc["documentId"]] = {
            r["index"]: _normalize(str(r.get("text") or ""))
            for page in doc.get("pages") or []
            for r in page.get("regions") or []
        }
    unverified = []
    for i, claim in enumerate(draft["claims"]):
        quote = _normalize(str(claim.get("quote") or ""))
        if claim.get("source") == "DOCUMENT" and claim.get("documentId") in texts:
            regions = texts[claim["documentId"]]
            haystack = (
                regions.get(claim.get("regionIndex"), "")
                if claim.get("regionIndex") is not None
                else "".join(regions.values())
            )
        else:
            haystack = statement
        claim["quoteVerified"] = bool(quote) and quote in haystack
        if not claim["quoteVerified"]:
            unverified.append(i)
    return unverified


def _add_document_follow_ups(draft: dict[str, Any], unread: list[int], truncated: list[int]) -> None:
    """읽지 못한 서류와 뒷쪽을 못 읽은 서류는 분석에 들어가지 않았으니 담당자가 직접 보도록 추가 확인 항목에 넣는다."""
    follow_ups = draft.setdefault("followUps", [])
    if unread:
        follow_ups.append(f"글자 인식에 실패한 증빙 {len(unread)}건(서류 {', '.join(map(str, unread))}) 원본 직접 확인")
    if truncated:
        follow_ups.append(f"쪽 수 상한으로 뒷쪽을 읽지 못한 증빙(서류 {', '.join(map(str, truncated))}) 원본 직접 확인")


def _clamp(draft: dict[str, Any]) -> None:
    """서버 검증 상한에 맞춰 자르고 빈 항목을 뺀다. 인젝션으로 긴 안내문을 유도해도 400 으로 실행 전체가 재시도되지 않게 한다."""

    def texts(values: list[Any], limit: int, count: int = LIST_LIMIT) -> list[str]:
        cleaned = [str(v).strip()[:limit] for v in values or [] if str(v or "").strip()]
        return cleaned[:count]

    draft["evidenceTransactionNumbers"] = texts(draft.get("evidenceTransactionNumbers"), TXNO_LIMIT, EVIDENCE_LIMIT)
    draft["missingDocuments"] = texts(draft.get("missingDocuments"), MISSING_DOCUMENT_LIMIT)
    draft["followUps"] = texts(draft.get("followUps"), FOLLOW_UP_LIMIT)
    draft["claims"] = list(draft.get("claims") or [])[:LIST_LIMIT]
    draft["contradictions"] = list(draft.get("contradictions") or [])[:LIST_LIMIT]
    draft["noticeDraft"] = str(draft.get("noticeDraft") or "")[:NOTICE_LIMIT]


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
    steps: StepRecorder,
    analysis: Analysis,
    dropped: dict[str, list[str]],
    adjusted: list[dict[str, str]] | None = None,
    unverified: list[int] | None = None,
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
            "unverifiedQuotes": unverified or [],
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
    if config.llm not in ("claude", "scripted"):
        raise SystemExit("AGENT_LLM 을 claude 또는 scripted 로 지정하세요. 운영은 claude 입니다.")
    server = ServerClient(config.server_base_url, config.worker_key)
    analyzer = build_analyzer(config)
    ocr = build_ocr(config)
    log.info("워커 시작 server=%s llm=%s model=%s ocr=%s", config.server_base_url, config.llm, config.model, config.ocr)
    while True:
        try:
            if not run_once(server, analyzer, config.step_delay_seconds, ocr, config.run_deadline_seconds):
                time.sleep(config.poll_seconds)
        except KeyboardInterrupt:
            break
        except Exception:  # 서버가 잠깐 내려가도 워커는 계속 돈다
            log.exception("작업 가져가기 실패")
            time.sleep(config.poll_seconds)


if __name__ == "__main__":
    main()
