"""API 키 없이 도는 규칙 분석기. 시연 리허설과 테스트용이며 Claude 분석기와 같은 도구 호출과 출력 모양을 낸다."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from objection_agent.llm.port import (
    EXTERNAL_CHECKS_TOOL_NAME,
    Analysis,
    ExternalChecksFn,
    LedgerFn,
    ToolCall,
    ToolCallback,
)

MODEL = "scripted-rules-v1"
RELATION_WORDS = ("지인", "친구", "가족", "거래처", "대금", "빌려", "갚")
NEGATIVE_REPLIES = ("INVOLVEMENT_SUSPECTED", "DOCUMENT_FORGED")

# 서류 글에서 금액과 일자를 뽑는 단순 규칙. OCR 이 쉼표를 마침표로 읽거나 빠뜨리는 경우(3000,000원)도 받는다
_AMOUNT = re.compile(r"(\d[\d,.]*\d|\d)\s*(만\s*)?원")
_DATE = re.compile(r"(20\d{2})\s*[.\-/년]\s*(\d{1,2})\s*[.\-/월]\s*(\d{1,2})")
# 이 이름표가 붙은 일자만 이체 일자로 보고 다툼 거래와 비교한다. 계약일 같은 다른 일자는 비교하지 않는다
_TRANSFER_DATE_LABELS = ("이체일", "송금일", "입금일", "거래일")


class ScriptedAnalyzer:
    def analyze(
        self,
        context: dict[str, Any],
        ledger: LedgerFn,
        on_tool_call: ToolCallback,
        external_checks: ExternalChecksFn | None = None,
    ) -> Analysis:
        disputed = context["disputedTransfers"]
        disputed_numbers = [item["transactionNumber"] for item in disputed]
        sender = next((item["counterpartyKey"] for item in disputed if item.get("counterpartyKey")), None)

        relation: list[dict[str, Any]] = []
        if sender:
            items = ledger(None, None, sender)
            on_tool_call(ToolCall("다툼 송금인과 다툼 이전에 오간 거래가 있는지 확인", {"counterparty": sender}, items))
            relation = [i for i in items if not i["disputed"] and not i["afterDispute"]]

        everything = ledger(None, None, None)
        on_tool_call(ToolCall("다툼 거래 전후의 입금과 출금 흐름 확인", {}, everything))
        fraud_numbers = _fraud_pattern(everything, disputed)

        replies: list[str] = []
        if external_checks is not None:
            checks = external_checks()
            on_tool_call(ToolCall("외부 기관 확인 회신이 있는지 확인", {}, checks, tool=EXTERNAL_CHECKS_TOOL_NAME))
            replies = [c["reply"] for c in checks if c.get("reply")]

        statement = context["statement"]
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", statement) if len(s.strip()) >= 8][:3]
        claims = [
            {"text": s.rstrip("."), "quote": s, "source": "STATEMENT", "documentId": None, "regionIndex": None}
            for s in sentences
        ]
        document_claims, document_contradictions = _documents(context.get("documents"), disputed)
        claims += document_claims

        contradictions = []
        relation_claim = next((s for s in sentences if any(w in s for w in RELATION_WORDS)), None)
        if relation_claim and len(relation) == 0:
            contradictions.append(
                {
                    "claim": relation_claim,
                    "ledgerFact": "다툼 이전에 이 송금인과 오간 당행 거래가 없음",
                    "transactionNumbers": disputed_numbers,
                }
            )

        contradictions += document_contradictions
        follow_ups: list[str] = []
        negative = [r for r in replies if r in NEGATIVE_REPLIES]

        if len(relation) >= 3 and (document_contradictions or negative):
            # 서류는 주장이라 근거를 올리지 못하고, 원장과 어긋나거나 외부 회신이 부정이면 수용 쪽으로 가지 않는다(D9)
            branch, evidence = "UNDETERMINED", []
            reason = (
                "외부 확인 회신이 부정적임"
                if negative
                else "다툼 이전 관계 거래는 있으나 제출 서류의 금액이나 일자가 원장과 달라 확인이 필요함"
            )
            follow_ups.append("제출 서류 진위 확인" if document_contradictions else "외부 확인 회신 내용 확인")
        elif len(relation) >= 3:
            branch, evidence = "NORMAL_ACCEPT", [i["transactionNumber"] for i in relation]
            reason = f"다툼 이전 송금인과의 거래 {len(relation)}회가 소명을 뒷받침함"
        elif fraud_numbers:
            branch, evidence = "FRAUD_PATTERN", fraud_numbers
            reason = "여러 송금인의 입금 직후 대부분이 빠져나감"
        else:
            branch, evidence = "UNDETERMINED", []
            reason = "소명을 뒷받침하거나 반박할 원장 근거가 부족함"

        missing = [doc for doc in context.get("commonDocuments", []) if doc.split()[0] not in statement][:3]
        draft = {
            "branch": branch,
            "branchReason": reason,
            "evidenceTransactionNumbers": evidence,
            "claims": claims,
            "contradictions": contradictions,
            "missingDocuments": missing,
            "followUps": follow_ups or (["송금인에게 거래 경위 확인"] if branch == "UNDETERMINED" else []),
            "noticeDraft": (
                "고객님의 이의제기를 접수해 심사하고 있습니다. "
                + ("아래 소명자료를 추가로 제출해 주시면 심사에 반영하겠습니다. " if missing else "")
                + "심사 결과는 확정되는 대로 안내드리겠습니다."
            ),
        }
        return Analysis(draft=draft, model=MODEL)


def _documents(
    documents: list[dict[str, Any]] | None, disputed: list[dict[str, Any]]
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """서류 줄에서 금액과 이체 일자 주장을 뽑고, 다툼 거래와 어긋나면 모순으로 남긴다."""
    amounts = {item["amount"] for item in disputed}
    dates = {item["occurredAt"][:10] for item in disputed}
    numbers = [item["transactionNumber"] for item in disputed]
    claims: list[dict[str, Any]] = []
    contradictions: list[dict[str, Any]] = []
    for doc in documents or []:
        found_amounts: list[tuple[int, dict[str, Any]]] = []
        found_dates: list[tuple[str, dict[str, Any]]] = []
        for page in doc.get("pages") or []:
            for region in page.get("regions") or []:
                text = str(region.get("text", ""))
                found_amounts += [(a, region) for a in parse_amounts(text)]
                compact = "".join(text.split())  # OCR 이 음절 사이를 띄우는 경우가 있어 공백을 빼고 이름표를 찾는다
                if any(label in compact for label in _TRANSFER_DATE_LABELS):
                    found_dates += [(d, region) for d in parse_dates(text)]
        doc_id = doc["documentId"]
        for amount, region in found_amounts[:2]:
            claims.append(_doc_claim(f"서류에 금액 {amount:,}원이 적혀 있음", region, doc_id))
        for date, region in found_dates[:1]:
            claims.append(_doc_claim(f"서류에 이체 일자 {date}가 적혀 있음", region, doc_id))
        if found_amounts and amounts and not any(a in amounts for a, _ in found_amounts):
            amount, region = found_amounts[0]
            contradictions.append(
                {
                    "claim": region["text"],
                    "ledgerFact": f"서류 금액 {amount:,}원이 다툼 거래 금액 {_won(amounts)}과 다름",
                    "transactionNumbers": numbers,
                }
            )
        if found_dates and dates and not any(d in dates for d, _ in found_dates):
            date, region = found_dates[0]
            contradictions.append(
                {
                    "claim": region["text"],
                    "ledgerFact": f"서류 이체 일자 {date}가 다툼 거래 일자 {', '.join(sorted(dates))}와 다름",
                    "transactionNumbers": numbers,
                }
            )
    return claims, contradictions


def _doc_claim(text: str, region: dict[str, Any], document_id: int) -> dict[str, Any]:
    return {
        "text": text,
        "quote": region["text"],
        "source": "DOCUMENT",
        "documentId": document_id,
        "regionIndex": region["index"],
    }


def _won(amounts: set[int]) -> str:
    return ", ".join(f"{a:,}원" for a in sorted(amounts))


def parse_amounts(text: str) -> list[int]:
    """`400,000원`, `40만원`, `1,000만 원` 꼴. 천 원 미만은 버린다."""
    found = [int(re.sub(r"[,.]", "", m.group(1))) * (10_000 if m.group(2) else 1) for m in _AMOUNT.finditer(text)]
    return [a for a in found if a >= 1000]


def parse_dates(text: str) -> list[str]:
    """`2026-11-05`, `2026.11.05`, `2026년 11월 5일` 꼴을 ISO 날짜로."""
    out = []
    for m in _DATE.finditer(text):
        try:
            out.append(datetime(int(m.group(1)), int(m.group(2)), int(m.group(3))).date().isoformat())
        except ValueError:
            continue
    return out


def _fraud_pattern(items: list[dict[str, Any]], disputed: list[dict[str, Any]]) -> list[str]:
    """서버 게이트와 같은 기준의 근사: 다툼 전후 24시간 입금 송금인 3명 이상, 입금 뒤 60분 안 출금이 80% 이상."""
    if not disputed:
        return []
    times = [_at(item["occurredAt"]) for item in disputed]
    start, end = min(times) - timedelta(hours=24), max(times) + timedelta(hours=24)
    deposits = [
        i for i in items if i["direction"] == "IN" and i["amount"] >= 10_000 and start <= _at(i["occurredAt"]) <= end
    ]
    senders = {i["counterpartyKey"] or f"ext:{i['transactionNumber']}" for i in deposits}
    withdrawals = [i for i in items if i["direction"] == "OUT"]
    used, total, covered = [i["transactionNumber"] for i in deposits], 0, 0
    remaining = {w["transactionNumber"]: w["amount"] for w in withdrawals}
    for deposit in deposits:
        total += deposit["amount"]
        need, at = deposit["amount"], _at(deposit["occurredAt"])
        for w in withdrawals:
            if need == 0 or remaining[w["transactionNumber"]] == 0:
                continue
            if at <= _at(w["occurredAt"]) <= at + timedelta(minutes=60):
                take = min(need, remaining[w["transactionNumber"]])
                remaining[w["transactionNumber"]] -= take
                need -= take
                covered += take
                if w["transactionNumber"] not in used:
                    used.append(w["transactionNumber"])
    return used if len(senders) >= 3 and total > 0 and covered * 100 >= total * 80 else []


def _at(value: str) -> datetime:
    return datetime.fromisoformat(value)
