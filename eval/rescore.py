"""저장된 평가 원자료에 판정안 보정 규칙(objection_agent.policy)을 적용해 다시 계산한다.

규칙은 모델 출력 뒤에 기계적으로 적용되므로, 같은 출력으로 다시 계산한 값이 규칙을 켜고 실제로 돌린 값과 같다.
  .venv/bin/python eval/rescore.py docs/results/261105/results-all.json <출력 json>
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from objection_agent.policy import apply_guards  # noqa: E402


def main() -> None:
    source, target = Path(sys.argv[1]), Path(sys.argv[2])
    runs = json.loads(source.read_text())
    changed = 0
    for run in runs:
        draft = run.get("draft") or {}
        probe = {"branch": run.get("agentBranch"), "contradictions": draft.get("contradictions") or []}
        # 저장된 GATE_EVALUATED 단계의 갈래가 워커가 판정안 직전에 받는 점검 갈래와 같다(같은 원장, 같은 규칙)
        if apply_guards(probe, (run.get("gate") or {}).get("branch")):
            run["agentBranch"] = probe["branch"]
            changed += 1
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(runs, ensure_ascii=False, indent=1))
    print(f"보정 규칙으로 바뀐 실행 {changed}건 / {len(runs)}건")


if __name__ == "__main__":
    main()
