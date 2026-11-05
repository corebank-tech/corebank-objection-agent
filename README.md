# 이의제기 Agent 워커

지급정지 이의제기 AI 심사 지원(D-36, PA-06)의 워커다. 서버 대기열에서 분석을 가져가 소명과 원장을 대조하고,
단계 이벤트를 남기며 판정안을 낸다. 해제와 판정 확정 권한은 없다. 서버 계약은
[objection_agent_contract.md](../../docs/phase2/objection_agent_contract.md), 단계 이벤트는
[agent_run_step_schema.md](../../docs/phase2/agent_run_step_schema.md)다.

## 흐름

1. `POST /internal/review-jobs/claim`으로 작업과 작업 토큰을 받는다. `CLAIMED`
2. 가명 처리한 소명과 다툼 거래를 읽는다. `CONTEXT_LOADED`
3. 분석기가 `get_ledger` 도구로 원장을 조회한다. 부를 때마다 `TOOL_CALL`
4. 주장과 모순을 남긴다. `CLAIM_EXTRACTED`, `CONTRADICTION`
5. 판정안을 낸다. `DRAFT_SUBMITTED`. 서버가 게이트를 다시 계산해 `GATE_EVALUATED`를 남긴다
6. 오류가 나면 `FAILED`를 남긴다. 시도가 남아 있으면 서버가 대기열로 돌린다

판정안 근거 거래번호는 분석기가 실제로 조회한 번호로만 걸러서 낸다.

## 분석기

| `AGENT_LLM` | 동작 |
|---|---|
| `scripted` (기본) | API 키 없이 도는 규칙 분석기. 서버 게이트와 같은 기준을 근사한다. 리허설과 테스트용 |
| `claude` | Claude 도구 루프. 마지막 답은 JSON 스키마 구조화 출력으로 받는다. 시스템 프롬프트를 캐시하고, 안전 분류기가 거절하면 서버 쪽 fallback(`fallbacks: "default"`)이 다른 모델로 다시 돌린다 |

## 환경변수

| 이름 | 기본값 | 뜻 |
|---|---|---|
| `CORE_SERVER_URL` | `http://localhost:8080/api/v1` | 코어 서버 주소 |
| `PAYMENT_STOP_WORKER_KEY` | (필수) | 서버 `app.payment-stop.agent.worker-key`와 같은 값 |
| `AGENT_LLM` | `scripted` | `scripted` 또는 `claude` |
| `ANTHROPIC_API_KEY` | | `claude`일 때 필수. 저장소에 두지 않는다 |
| `AGENT_MODEL` | `claude-opus-5-5` | |
| `AGENT_EFFORT` | `low` | |
| `AGENT_POLL_SECONDS` | `2` | 대기열이 비었을 때 다시 볼 간격 |
| `AGENT_STEP_DELAY_SECONDS` | `0` | 단계 사이 간격. 시연에서 타임라인이 한 줄씩 보이게 할 때 쓴다 |
| `AGENT_MAX_TOOL_TURNS` | `8` | 도구 호출 상한 |

서버는 `PAYMENT_STOP_AGENT_ENABLED=true`와 같은 `PAYMENT_STOP_WORKER_KEY`로 띄운다. 킬스위치가 꺼져 있으면 작업이 오지 않는다.

## 실행

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q
PAYMENT_STOP_WORKER_KEY=... .venv/bin/python -m objection_agent
```

비용은 `costMicroUsd`(백만분의 1달러)로 판정안과 함께 서버에 남는다. 단가는 `llm/claude.py`의 `PRICE_MICRO_USD`다.
