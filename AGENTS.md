# AGENTS.md

코딩 에이전트용 진입점. 사람용 개요는 [README.md](README.md)다.

## 명령

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m pytest -q                  # 워커 테스트(가짜 서버, 가짜 Anthropic 응답)
.venv/bin/python -m objection_agent            # 워커 실행. 환경변수는 README
.venv/bin/python eval/analyze.py <results.json> claude-opus   # 평가 지표
```

## 규칙

1. **계약 정본은 corebank-server다.** 내부 API 경로, 필드, 단계 종류는 `docs/phase2/objection_agent_contract.md`와 `agent_run_step_schema.md`를 따른다. 여기서 먼저 바꾸지 않는다.
2. **Agent는 해제와 판정 확정을 하지 않는다.** 읽기, 단계 기록, 판정안 제출만 한다. 근거 거래번호는 실제로 조회한 번호만 낸다.
3. **키를 커밋하지 않는다.** `.env`와 환경변수만 쓴다. pre-commit gitleaks를 우회하지 않는다(`--no-verify` 금지).
4. **평가 정답은 실행 전에 정하고 결과를 본 뒤 고치지 않는다.** 정답이 틀렸다고 판단되면 고치지 말고 보고서의 결과 해석에 적는다.
5. **암호화 경로를 지킨다.** 프롬프트와 공격 시험 문장, 소명이 든 원자료는 `.gitattributes`의 git-crypt 경로에만 둔다. 새 민감 파일은 `eval/private/` 아래에 만들고, 커밋 전에 `git-crypt status -e`로 확인한다. 보고서와 지표에 공격 문장 원문을 옮겨 적지 않는다.
6. **실제 API 호출은 비용이 든다.** 평가를 다시 돌리기 전에 사람에게 예상 비용을 알리고 승인을 받는다.
