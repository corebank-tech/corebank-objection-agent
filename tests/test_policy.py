from objection_agent.policy import apply_branch_guard, apply_gate_guard, apply_guards


def draft(branch, contradictions):
    return {"branch": branch, "branchReason": "근거 있음", "contradictions": contradictions, "followUps": []}


def test_contradiction_turns_accept_into_hold():
    d = draft("NORMAL_ACCEPT", [{"claim": "매주", "ledgerFact": "석 달에 3번", "transactionNumbers": []}])

    adjusted = apply_branch_guard(d)

    assert adjusted == {"from": "NORMAL_ACCEPT", "to": "UNDETERMINED", "rule": "CONTRADICTION_BLOCKS_ACCEPT"}
    assert d["branch"] == "UNDETERMINED"
    assert d["branchReason"].startswith("소명과 원장이 어긋나 판단 보류")
    assert "소명과 원장의 불일치 경위 확인" in d["followUps"]


def test_partial_is_also_held():
    assert apply_branch_guard(draft("NORMAL_PARTIAL", [{"claim": "a"}]))["from"] == "NORMAL_PARTIAL"


def test_no_contradiction_or_non_accept_branch_is_untouched():
    for d in (
        draft("NORMAL_ACCEPT", []),
        draft("FRAUD_PATTERN", [{"claim": "a"}]),
        draft("UNDETERMINED", [{"claim": "a"}]),
    ):
        before = dict(d)
        assert apply_branch_guard(d) is None
        assert d == before


def test_gate_guard_holds_or_narrows():
    held = draft("NORMAL_ACCEPT", [])
    assert apply_gate_guard(held, "UNDETERMINED")["to"] == "UNDETERMINED"
    assert held["branch"] == "UNDETERMINED"

    narrowed = draft("NORMAL_ACCEPT", [])
    assert apply_gate_guard(narrowed, "NORMAL_PARTIAL")["to"] == "NORMAL_PARTIAL"

    fraud_gate = draft("NORMAL_PARTIAL", [])
    assert apply_gate_guard(fraud_gate, "FRAUD_PATTERN")["to"] == "UNDETERMINED"


def test_gate_guard_leaves_agreeing_conservative_or_unknown_cases():
    assert apply_gate_guard(draft("NORMAL_ACCEPT", []), "NORMAL_ACCEPT") is None
    assert apply_gate_guard(draft("NORMAL_PARTIAL", []), "NORMAL_ACCEPT") is None
    assert apply_gate_guard(draft("FRAUD_PATTERN", []), "NORMAL_ACCEPT") is None
    assert apply_gate_guard(draft("UNDETERMINED", []), "NORMAL_ACCEPT") is None
    assert apply_gate_guard(draft("NORMAL_ACCEPT", []), None) is None


def test_guards_apply_in_order_and_record_each_change():
    d = draft("NORMAL_ACCEPT", [{"claim": "a"}])
    assert [a["rule"] for a in apply_guards(d, "UNDETERMINED")] == ["CONTRADICTION_BLOCKS_ACCEPT"]
    assert d["branch"] == "UNDETERMINED"
