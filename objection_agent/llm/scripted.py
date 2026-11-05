"""API 키 없이 도는 규칙 분석기. 시연 리허설과 테스트용이며 Claude 분석기와 같은 도구 호출과 출력 모양을 낸다."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any

from objection_agent.llm.port import Analysis, LedgerFn, ToolCall, ToolCallback

MODEL = "scripted-rules-v1"
RELATION_WORDS = ("지인", "친구", "가족", "거래처", "대금", "빌려", "갚")


class ScriptedAnalyzer:
    def analyze(self, context: dict[str, Any], ledger: LedgerFn, on_tool_call: ToolCallback) -> Analysis:
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

        statement = context["statement"]
        sentences = [s.strip() for s in re.split(r"(?<=[.!?])\s+|\n+", statement) if len(s.strip()) >= 8][:3]
        claims = [{"text": s.rstrip("."), "quote": s} for s in sentences]

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

        if len(relation) >= 3:
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
            "followUps": ["송금인에게 거래 경위 확인"] if branch == "UNDETERMINED" else [],
            "noticeDraft": (
                "고객님의 이의제기를 접수해 심사하고 있습니다. "
                + ("아래 소명자료를 추가로 제출해 주시면 심사에 반영하겠습니다. " if missing else "")
                + "심사 결과는 확정되는 대로 안내드리겠습니다."
            ),
        }
        return Analysis(draft=draft, model=MODEL)


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
