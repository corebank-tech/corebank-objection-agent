from objection_agent.llm.scripted import ScriptedAnalyzer
from objection_agent.server import ServerClient
from objection_agent.worker import run_once
from tests.fake_server import DISPUTED, FakeServer, mule_ledger, relationship_ledger


def client(fake: FakeServer) -> ServerClient:
    return ServerClient("http://core/api/v1", "key", transport=fake.transport())


def test_relationship_case_submits_normal_accept_with_prior_transfers_only():
    fake = FakeServer(relationship_ledger(), "친구에게 빌려준 돈을 돌려받았습니다. 매달 주고받은 내역이 있습니다.")

    assert run_once(client(fake), ScriptedAnalyzer()) is True

    types = [s["type"] for s in fake.steps]
    assert types == ["CLAIMED", "CONTEXT_LOADED", "TOOL_CALL", "TOOL_CALL", "CLAIM_EXTRACTED", "DRAFT_SUBMITTED"]
    draft = fake.drafts[0]
    assert draft["branch"] == "NORMAL_ACCEPT"
    assert DISPUTED not in draft["evidenceTransactionNumbers"]
    assert len(draft["evidenceTransactionNumbers"]) == 3
    assert draft["promptVersion"] == "objection-v2.1"
    tool = fake.steps[2]["detail"]
    assert tool["tool"] == "get_ledger" and tool["args"] == {"counterparty": "CP-0000000a"}


def test_mule_case_submits_fraud_pattern_and_contradiction():
    fake = FakeServer(mule_ledger(), "지인에게 받은 거래 대금입니다. 사기와 관련이 없습니다.")

    run_once(client(fake), ScriptedAnalyzer())

    assert fake.drafts[0]["branch"] == "FRAUD_PATTERN"
    contradiction = next(s for s in fake.steps if s["type"] == "CONTRADICTION")
    assert contradiction["detail"]["transactionNumbers"] == [DISPUTED]


def test_empty_queue_returns_false():
    fake = FakeServer(relationship_ledger(), "소명")
    fake.queue.clear()

    assert run_once(client(fake), ScriptedAnalyzer()) is False
    assert fake.steps == []


def test_rejected_draft_records_failed_step():
    fake = FakeServer(relationship_ledger(), "친구에게 빌려준 돈입니다.", fail_draft_code="PST0005")

    run_once(client(fake), ScriptedAnalyzer())

    assert fake.steps[-1]["type"] == "FAILED"
    assert fake.steps[-1]["status"] == "FAILED"
    assert "PST0005" in fake.steps[-1]["summary"]


def test_expired_token_does_not_record_more_steps():
    fake = FakeServer(relationship_ledger(), "친구에게 빌려준 돈입니다.", fail_draft_code="PST0102")

    run_once(client(fake), ScriptedAnalyzer())

    assert fake.steps[-1]["type"] == "DRAFT_SUBMITTED"


def test_step_starts_when_previous_step_ended():
    fake = FakeServer(relationship_ledger(), "친구에게 빌려준 돈입니다.")

    run_once(client(fake), ScriptedAnalyzer())

    for previous, step in zip(fake.steps, fake.steps[1:], strict=False):
        assert step["startedAt"] >= previous["endedAt"]
        assert step["endedAt"] >= step["startedAt"]
