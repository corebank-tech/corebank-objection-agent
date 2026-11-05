# corebank-objection-agent

지급정지 이의제기 AI 심사 지원(D-36)의 Agent 워커와 평가. 서버 대기열에서 분석을 가져가 소명과 원장을 대조하고,
단계 이벤트를 남기며 판정안을 낸다. 해제와 판정 확정 권한은 없다. 해제는 서버의 해제 게이트와 담당자가 정한다.

| 위치 | 내용 |
|---|---|
| [corebank-server](https://github.com/corebank-tech/corebank-server) | 지급정지 도메인, 해제 게이트, 내부 API, 분석 대기열(PR #919). **계약 정본** |
| [corebank-fe](https://github.com/corebank-tech/corebank-fe) | 관리자 심사 화면과 실행 타임라인(PR #213) |
| 이 레포 | 워커(`objection_agent/`), 평가(`eval/`), 평가 결과(`docs/results/`) |

서버 계약은 corebank-server의 `docs/phase2/objection_agent_contract.md`, 단계 이벤트는 `docs/phase2/agent_run_step_schema.md`가 정본이다.
PR #919가 dev에 머지되기 전에는 [브랜치의 계약 문서](https://github.com/corebank-tech/corebank-server/blob/feat/paymentstop-agent-api/docs/phase2/objection_agent_contract.md)를 본다.
계약을 바꿀 때는 서버 정본을 먼저 고친다.

평가 결과(11/5): [성과 수치](docs/results/261105/performance.md), [평가 보고서](docs/results/261105/evaluation.md).

## 호환 버전

서버와 레포가 나뉘어 있어 맞는 조합을 여기 적는다. 서버 내부 API 계약이 바뀌면 이 표를 함께 고친다.

| 이 레포 | corebank-server | 비고 |
|---|---|---|
| `main` | PR #919 `feat/paymentstop-agent-api` 커밋 `23cc5067` 이후 | 내부 API 계약 `docs/phase2/objection_agent_contract.md` |
| `aa72a65` | 위와 같음 | 11/6 시연 환경에 올라간 워커 |

평가 시드(`eval/seed.sql`)는 서버 Flyway `V202611051600`까지의 `customer`, `account`, `transfer`, `ledger_entry` 컬럼을 가정한다. 서버에서 이 테이블이 바뀌면 시드를 다시 만든다.

## 암호화 파일

프롬프트(`objection_agent/prompts.py`), 평가 시나리오와 정답(`eval/scenarios.py`, `eval/manifest.json`, `eval/private/`), 평가 원자료(`docs/results/**/results-*.json`)는
[git-crypt](https://github.com/AGWA/git-crypt)로 암호화돼 있다. 키 없이 클론하면 이 파일들은 바이너리로 보이고 워커와 평가를 실행할 수 없다.
코드, 평가 스크립트, 지표(`metrics.json`)와 보고서는 공개다.

```bash
brew install git-crypt
git-crypt unlock /path/to/git-crypt.key   # 키는 담당자에게 받는다. 레포에 넣지 않는다
```

암호화 대상은 `.gitattributes`가 정한다. 새 민감 파일(공격 시나리오 등)은 `eval/private/` 아래에 둔다. 커밋 전에 `git-crypt status -e`로 암호화 여부를 확인한다.

## 개발 규칙

- 처음 한 번 `git config core.hooksPath .githooks` — 커밋 전에 gitleaks가 자격증명을 검사한다(`brew install gitleaks`). 서버 레포와 같은 훅이다.
- 키는 `.env`(무시 대상)나 환경변수로만 둔다. 레포에 넣지 않는다.
- `main`에 직접 push하지 않고 PR로 올린다. CI가 pytest, 평가 스크립트 컴파일, 저장된 평가 결과의 지표 재현, gitleaks를 돈다.
- 서버 내부 API를 바꿔야 하면 corebank-server의 계약 문서와 구현을 먼저 고치고, 이 레포는 그 뒤에 맞춘다.

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
docker build -t objection-worker .   # 컨테이너로 돌릴 때
```

비용은 `costMicroUsd`(백만분의 1달러)로 판정안과 함께 서버에 남는다. 단가는 `llm/claude.py`의 `PRICE_MICRO_USD`다.
