# 평가

합성 시나리오 24건으로 Agent 판정안을 평가한다. 정답(갈래, 심어 둔 모순 여부)은 실행 전에 `scenarios.py`에 적고 `manifest.json`으로 고정한다.

| 파일 | 역할 |
|---|---|
| `scenarios.py` | 시나리오 정의와 정답 → `seed.sql`(계좌, 이체, 원장), `manifest.json` |
| `run_eval.py` | `register`는 사건 등록과 이의제기 접수, `drive N`은 건마다 실행이 N회가 되도록 재분석 요청, `collect`는 결과 수집 |
| `run_round.sh` | 워커를 띄워 한 회차를 돌리고 결과 파일을 남긴다 |
| `analyze.py` | 지표(Wilson 95% 신뢰구간)와 보고서 → 결과 파일 옆에 `metrics.json`, `report.md` |
| `timing_sheet.csv` | 담당자 처리시간 교차 평가 기록 양식(`docs/timing_protocol.md`) |

## 재현

1. corebank-server(PR #919 브랜치)를 평가 전용 MySQL에 `local` 프로필로 띄운다. `PAYMENT_STOP_AGENT_ENABLED=true`, `PAYMENT_STOP_WORKER_KEY=<키>`.
   운영이나 팀 공용 DB에 넣지 않는다.
2. 시드 적재: `docker exec -i <mysql 컨테이너> mysql -uroot -p<비밀번호> minicore < eval/seed.sql`
3. 레포 루트 `.env`에 `PAYMENT_STOP_WORKER_KEY`, `ANTHROPIC_API_KEY`
4. 실행

```bash
.venv/bin/python eval/run_eval.py register
eval/run_round.sh claude-opus-5-5 3 2 results-opus.json      # 건당 3회
eval/run_round.sh claude-sonnet-5-5 4 2 results-sonnet.json  # 누적 4회째 = Sonnet 1회
eval/run_round.sh claude-haiku-5-5 5 2 results-haiku.json
eval/run_round.sh scripted 6 1 results-all.json
.venv/bin/python eval/analyze.py eval/results-all.json claude-opus claude-sonnet claude-haiku scripted
```

환경변수: `EVAL_SERVER_URL`(기본 `http://localhost:8080/api/v1`), `EVAL_MYSQL_CONTAINER`, `EVAL_MYSQL_PASSWORD`, `EVAL_ADMIN_PASSWORD`.
11/5 실행에서 Opus 72회와 비교 72회에 $3.40이 들었다.

데이터 확인: 분석을 돌리기 전에 관리자 상세 API의 해제 게이트 결과가 시나리오마다 `expected_gate`와 같은지 본다. 다르면 시드가 의도와 다르게 만들어진 것이다.

## 합성 증빙 서류(PA-13)

`documents.py`는 시연과 평가용 증빙 PNG를 Pillow로 그린다(개발 의존성). 내용은 전부 가상이고 모든 장에 「시연용 견본」을 찍는다. 신분증 사본은 만들지 않는다.

```bash
.venv/bin/python eval/documents.py                                  # eval/documents/*.png (커밋하지 않는다)
.venv/bin/python eval/documents.py --amount 300000 --date 2026-11-05 --out /tmp/docs
```

| 파일 | 내용 |
|---|---|
| `loan_contract.png` | 금전 소비대차 계약서. 매월 상환액이 다툼 거래 금액과 같다 |
| `transfer_receipt.png` | 이체 확인증. 이체 일시와 금액이 다툼 거래와 같다 |
| `used_goods_receipt.png` | 중고 물품 거래 영수증. 거래일과 판매금액이 다툼 거래와 같다 |
| `forged_receipt.png` | 위조 이체 확인증. 금액(10배)과 이체 일자(8일 전)가 원장과 어긋난다. Agent는 모순으로, 게이트는 수용을 막아야 한다 |

한국어 글꼴이 필요하다. macOS 기본 글꼴(`AppleSDGothicNeo`)이나 Linux `fonts-nanum`을 찾고, 다른 글꼴은 `EVAL_DOCUMENT_FONT`로 준다. 없으면 오류로 멈춘다.
기존 24건 시나리오(`scenarios.py`, `seed.sql`, `manifest.json`)는 바꾸지 않았다. 서류는 관리자 업로드 API로 이의제기에 붙인다.
