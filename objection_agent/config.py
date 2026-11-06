"""환경변수 설정. 키는 저장소에 두지 않고 환경변수로만 받는다."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    server_base_url: str
    worker_key: str
    # "claude" 는 실제 호출, "scripted" 는 API 키 없이 도는 규칙 분석기(시연 리허설, 테스트). 비어 있으면 워커가 시작하지 않는다.
    # 운영에서 설정을 빠뜨려 규칙 분석기 판정안이 실제 사건에 들어가는 것을 막는다
    llm: str
    model: str
    effort: str
    poll_seconds: float
    step_delay_seconds: float  # 시연에서 타임라인이 한 줄씩 보이도록 단계 사이에 두는 간격
    max_tool_turns: int
    ocr: str = "tesseract"  # "tesseract" 는 로컬 Tesseract CLI, "off" 는 서류 글자 인식을 건너뛴다
    ocr_lang: str = "kor+eng"
    ocr_max_pages: int = 10  # PDF 한 건에서 읽는 최대 쪽 수
    ocr_preprocess: bool = True  # 방향, 기울기, 조명, 잡음 보정(objection_agent.preprocess)
    # 실행 전체 마감(초). 서버 작업 토큰 유효 시간(10분)보다 짧아야 토큰이 살아 있을 때 실패를 남긴다
    run_deadline_seconds: float = 480.0

    @staticmethod
    def from_env() -> Config:
        return Config(
            server_base_url=os.environ.get("CORE_SERVER_URL", "http://localhost:8080/api/v1").rstrip("/"),
            worker_key=os.environ.get("PAYMENT_STOP_WORKER_KEY", ""),
            llm=os.environ.get("AGENT_LLM", ""),
            model=os.environ.get("AGENT_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("AGENT_EFFORT", "low"),
            poll_seconds=float(os.environ.get("AGENT_POLL_SECONDS", "2")),
            step_delay_seconds=float(os.environ.get("AGENT_STEP_DELAY_SECONDS", "0")),
            max_tool_turns=int(os.environ.get("AGENT_MAX_TOOL_TURNS", "8")),
            ocr=os.environ.get("AGENT_OCR", "tesseract"),
            ocr_lang=os.environ.get("AGENT_OCR_LANG", "kor+eng"),
            ocr_max_pages=int(os.environ.get("AGENT_OCR_MAX_PAGES", "10")),
            ocr_preprocess=os.environ.get("AGENT_OCR_PREPROCESS", "true").lower() != "false",
            run_deadline_seconds=float(os.environ.get("AGENT_RUN_DEADLINE_SECONDS", "480")),
        )
