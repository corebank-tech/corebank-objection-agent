"""Claude 분석기의 도구 루프. 실제 API 를 부르지 않고 응답 객체를 흉내 낸다."""

import json
from types import SimpleNamespace as NS

import pytest

from objection_agent.llm.claude import ClaudeAnalyzer, cost_micro_usd
from objection_agent.llm.port import AnalysisRefused, Usage
from objection_agent.server import ServerClient
from objection_agent.worker import run_once
from tests.fake_server import DISPUTED, FakeServer, relationship_ledger

DRAFT = {
    "branch": "NORMAL_ACCEPT",
    "branchReason": "다툼 이전 관계 거래 3회",
    "evidenceTransactionNumbers": ["20991231WB0000000001", "20991231WB0000000002", "20991231WB9999999999", DISPUTED],
    "claims": [{"text": "친구에게 빌려준 돈", "quote": "친구에게 빌려준 돈입니다."}],
    "contradictions": [],
    "missingDocuments": [],
    "followUps": [],
    "noticeDraft": "심사 중입니다.",
}


def usage(i, o, read=0, write=0):
    return NS(input_tokens=i, output_tokens=o, cache_read_input_tokens=read, cache_creation_input_tokens=write)


class FakeMessages:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.responses.pop(0)


def fake_client(responses):
    messages = FakeMessages(responses)
    return NS(beta=NS(messages=messages)), messages


def tool_use_response():
    block = NS(
        type="tool_use",
        id="tu_1",
        name="get_ledger",
        input={"reason": "송금인과의 과거 거래 확인", "from": None, "to": None, "counterparty": "CP-0000000a"},
    )
    return NS(
        stop_reason="tool_use",
        content=[NS(type="thinking", thinking=""), block],
        usage=usage(1000, 50, write=900),
        model="claude-opus-5-5",
    )


def final_response(draft=DRAFT):
    return NS(
        stop_reason="end_turn",
        content=[NS(type="text", text=json.dumps(draft, ensure_ascii=False))],
        usage=usage(200, 300, read=900),
        model="claude-opus-5-5",
    )


def test_tool_loop_records_tool_call_and_drops_unseen_evidence():
    client, messages = fake_client([tool_use_response(), final_response()])
    fake = FakeServer(relationship_ledger(), "친구에게 빌려준 돈입니다.")

    run_once(
        ServerClient("http://core/api/v1", "key", transport=fake.transport()),
        ClaudeAnalyzer("claude-opus-5-5", "low", 8, client),
    )

    assert [s["type"] for s in fake.steps] == [
        "CLAIMED",
        "CONTEXT_LOADED",
        "TOOL_CALL",
        "CLAIM_EXTRACTED",
        "DRAFT_SUBMITTED",
    ]
    draft = fake.drafts[0]
    # 도구가 돌려주지 않은 번호와 정상 갈래의 다툼 거래는 빠진다
    assert draft["evidenceTransactionNumbers"] == ["20991231WB0000000001", "20991231WB0000000002"]
    dropped = fake.steps[-1]["detail"]["droppedTransactionNumbers"]
    assert dropped == {"unseen": ["20991231WB9999999999"], "tainted": [DISPUTED]}
    assert draft["inputTokens"] == 1000 + 200 + 900 + 900
    assert draft["costMicroUsd"] == cost_micro_usd(Usage(1200, 350, 900, 900))

    first, second = messages.calls
    assert first["fallbacks"] == "default"
    assert first["output_config"]["format"]["type"] == "json_schema"
    assert first["system"][0]["cache_control"] == {"type": "ephemeral"}
    # 사고 블록을 포함한 앞선 응답을 그대로 돌려준다
    assert second["messages"][1]["role"] == "assistant"
    assert [b.type for b in second["messages"][1]["content"]] == ["thinking", "tool_use"]
    tool_result = second["messages"][2]["content"][0]
    assert tool_result["tool_use_id"] == "tu_1"
    assert DISPUTED in tool_result["content"]


def test_refusal_becomes_failed_step():
    refusal = NS(stop_reason="refusal", content=[], usage=usage(100, 0), model="claude-opus-5-5")
    client, _ = fake_client([refusal])
    fake = FakeServer(relationship_ledger(), "친구에게 빌려준 돈입니다.")

    run_once(
        ServerClient("http://core/api/v1", "key", transport=fake.transport()),
        ClaudeAnalyzer("claude-opus-5-5", "low", 8, client),
    )

    assert fake.steps[-1]["type"] == "FAILED"
    assert fake.drafts == []


def test_tool_turn_limit():
    client, _ = fake_client([tool_use_response(), tool_use_response()])
    analyzer = ClaudeAnalyzer("claude-opus-5-5", "low", 1, client)

    with pytest.raises(AnalysisRefused):
        analyzer.analyze(
            {
                "clause": 1,
                "simplifiedRequested": False,
                "idCopyConfirmed": True,
                "litigationPending": False,
                "ledgerFrom": "a",
                "ledgerTo": "b",
                "disputedTransfers": [],
                "statement": "s",
            },
            lambda *_: [],
            lambda _: None,
        )


def test_cost_in_micro_usd():
    assert cost_micro_usd(Usage(1_000_000, 0)) == 4_000_000
    assert cost_micro_usd(Usage(0, 1000, 1000, 1000)) == 20_000 + 200 + 5000


def test_contradiction_guard_holds_accept_before_submission():
    contradicted = {
        **DRAFT,
        "contradictions": [
            {"claim": "매주 정산", "ledgerFact": "석 달에 3번", "transactionNumbers": ["20991231WB0000000001"]}
        ],
    }
    client, _ = fake_client([tool_use_response(), final_response(contradicted)])
    fake = FakeServer(relationship_ledger(), "친구와 매주 정산했습니다.")

    run_once(
        ServerClient("http://core/api/v1", "key", transport=fake.transport()),
        ClaudeAnalyzer("claude-opus-5-5", "low", 8, client),
    )

    assert fake.drafts[0]["branch"] == "UNDETERMINED"
    submitted = fake.steps[-1]["detail"]
    assert submitted["branch"] == "UNDETERMINED"
    assert submitted["branchAdjustments"] == [
        {"from": "NORMAL_ACCEPT", "to": "UNDETERMINED", "rule": "CONTRADICTION_BLOCKS_ACCEPT"}
    ]


def test_gate_guard_holds_accept_without_release_ground():
    client, _ = fake_client([tool_use_response(), final_response()])
    fake = FakeServer(relationship_ledger(), "친구와 자주 주고받았습니다.", gate_branch="UNDETERMINED")

    run_once(
        ServerClient("http://core/api/v1", "key", transport=fake.transport()),
        ClaudeAnalyzer("claude-opus-5-5", "low", 8, client),
    )

    assert fake.drafts[0]["branch"] == "UNDETERMINED"
    assert fake.steps[-1]["detail"]["branchAdjustments"] == [
        {"from": "NORMAL_ACCEPT", "to": "UNDETERMINED", "rule": "GATE_LIMITS_ACCEPT"}
    ]


def test_gate_guard_keeps_accept_when_gate_agrees():
    client, _ = fake_client([tool_use_response(), final_response()])
    fake = FakeServer(relationship_ledger(), "친구에게 빌려준 돈입니다.", gate_branch="NORMAL_ACCEPT")

    run_once(
        ServerClient("http://core/api/v1", "key", transport=fake.transport()),
        ClaudeAnalyzer("claude-opus-5-5", "low", 8, client),
    )

    assert fake.drafts[0]["branch"] == "NORMAL_ACCEPT"
    assert fake.steps[-1]["detail"]["branchAdjustments"] == []
