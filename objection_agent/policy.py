"""판정안 보정 규칙. 모델 출력 뒤에 기계적으로 적용하는 업무 규칙이다.

D9: 주장은 해제 근거를 올리지 못하고, 원장과 모순될 때만 신호가 된다. 그래서 소명이나 서류의 주장이 원장과 하나라도 어긋나면
수용 계열(수용, 일부 지급정지) 판정안을 내지 않고 판단 보류로 돌린다. 모델이 모순을 찾아 놓고도 "경미하다"며 수용 후보를
내는 사례(11/5 평가 C3)를 막는다. 해제 여부는 여전히 해제 요건 점검과 담당자가 정한다.

규칙 2(GATE_LIMITS_ACCEPT): 해제 요건 점검이 수용 근거를 인정하지 않으면 AI 의 수용 계열 판정안을 판단 보류로, 일부 지급정지만
인정하면 일부 지급정지로 낮춘다. 은행의 수용 기준(관계 거래 3회 이상 등, 내부 기준)을 모르는 모델이 근거가 모자란 사건을 수용 후보로
내는 사례(11/6 홀드아웃 B1)를 막는다. 모델은 판정하는 동안 점검 결과를 보지 못하고, 판정안을 낸 뒤 기계적으로만 적용한다.
AI 가 점검보다 보수적인 판정(판단 보류, 사기 패턴)을 내는 것은 그대로 둔다.
"""

from __future__ import annotations

from typing import Any

ACCEPT_BRANCHES = ("NORMAL_ACCEPT", "NORMAL_PARTIAL")
HOLD_BRANCH = "UNDETERMINED"
RULE_CONTRADICTION = "CONTRADICTION_BLOCKS_ACCEPT"
RULE_GATE = "GATE_LIMITS_ACCEPT"


def apply_branch_guard(draft: dict[str, Any]) -> dict[str, str] | None:
    """규칙에 걸리면 판정안을 고치고 무엇을 바꿨는지 돌려준다. 안 걸리면 None."""
    if draft.get("branch") not in ACCEPT_BRANCHES or not draft.get("contradictions"):
        return None
    before = draft["branch"]
    draft["branch"] = HOLD_BRANCH
    draft["branchReason"] = f"소명과 원장이 어긋나 판단 보류(모순 {len(draft['contradictions'])}건). " + (
        draft.get("branchReason") or ""
    )
    follow_ups = draft.setdefault("followUps", [])
    if "소명과 원장의 불일치 경위 확인" not in follow_ups:
        follow_ups.append("소명과 원장의 불일치 경위 확인")
    return {"from": before, "to": HOLD_BRANCH, "rule": RULE_CONTRADICTION}


def apply_gate_guard(draft: dict[str, Any], gate_branch: str | None) -> dict[str, str] | None:
    """해제 요건 점검 갈래로 수용 계열 판정안을 제한한다. gate_branch 가 없으면(옛 서버) 적용하지 않는다."""
    branch = draft.get("branch")
    if gate_branch is None or branch not in ACCEPT_BRANCHES:
        return None
    if branch == "NORMAL_ACCEPT" and gate_branch == "NORMAL_PARTIAL":
        target = "NORMAL_PARTIAL"
    elif gate_branch in ACCEPT_BRANCHES:
        return None
    else:
        target = HOLD_BRANCH
    draft["branch"] = target
    draft["branchReason"] = f"해제 요건 점검 결과({gate_branch})에 맞춰 조정. " + (draft.get("branchReason") or "")
    return {"from": branch, "to": target, "rule": RULE_GATE}


NOTICE_FOLLOW_UP = "보정 규칙으로 갈래가 바뀜. 안내문 초안은 바뀌기 전 갈래 기준이므로 최종 판정에 맞춰 확인"


def apply_guards(draft: dict[str, Any], gate_branch: str | None) -> list[dict[str, str]]:
    """보정 규칙을 순서대로 적용하고 바뀐 내역을 돌려준다.

    안내문 초안은 모델이 원래 갈래로 쓴 글이라 그대로 두되(자료 요청 같은 쓸 만한 내용이 있다), 담당자가 최종 판정에 맞춰
    다시 보도록 추가 확인 항목을 붙인다.
    """
    adjusted = [a for a in (apply_branch_guard(draft), apply_gate_guard(draft, gate_branch)) if a]
    if adjusted and NOTICE_FOLLOW_UP not in draft.setdefault("followUps", []):
        draft["followUps"].append(NOTICE_FOLLOW_UP)
    return adjusted
