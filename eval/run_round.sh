#!/bin/zsh
# 워커 N개를 띄우고 건마다 실행이 TARGET 회가 될 때까지 돌린 뒤 결과를 모은다.
#   eval/run_round.sh <모델|scripted> <누적 실행 횟수> <워커 수> <결과 파일>
# 레포 루트의 .env 에 PAYMENT_STOP_WORKER_KEY(서버와 같은 값), ANTHROPIC_API_KEY 를 둔다(.gitignore 대상).
set -euo pipefail
root="$(cd "$(dirname "$0")/.." && pwd)"
model="$1"; target="$2"; workers="$3"; out="$4"
set -a; source "$root/.env"; set +a
if [[ "$model" == scripted ]]; then export AGENT_LLM=scripted; else export AGENT_LLM=claude AGENT_MODEL="$model"; fi
export AGENT_STEP_DELAY_SECONDS=0
pids=()
for i in $(seq 1 "$workers"); do
  (cd "$root" && exec .venv/bin/python -m objection_agent) > "/tmp/eval-worker-$i.log" 2>&1 &
  pids+=($!)
done
trap 'kill ${pids[@]} 2>/dev/null || true' EXIT
"$root/.venv/bin/python" "$root/eval/run_eval.py" drive "$target"
"$root/.venv/bin/python" "$root/eval/run_eval.py" collect
cp "$root/eval/results.json" "$root/eval/$out"
echo "done $out"
