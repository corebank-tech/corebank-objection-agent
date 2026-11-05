"""환경변수 설정. 키는 저장소에 두지 않고 환경변수로만 받는다."""

from __future__ import annotations

import os
from dataclasses import dataclass


@dataclass(frozen=True)
class Config:
    server_base_url: str
    worker_key: str
    llm: str  # "scripted" 는 API 키 없이 도는 녹화 응답, "claude" 는 실제 호출
    model: str
    effort: str
    poll_seconds: float
    step_delay_seconds: float  # 시연에서 타임라인이 한 줄씩 보이도록 단계 사이에 두는 간격
    max_tool_turns: int
    ocr: str = "tesseract"  # "tesseract" 는 로컬 Tesseract CLI, "off" 는 서류 글자 인식을 건너뛴다
    ocr_lang: str = "kor+eng"
    ocr_max_pages: int = 10  # PDF 한 건에서 읽는 최대 쪽 수

    @staticmethod
    def from_env() -> Config:
        return Config(
            server_base_url=os.environ.get("CORE_SERVER_URL", "http://localhost:8080/api/v1").rstrip("/"),
            worker_key=os.environ.get("PAYMENT_STOP_WORKER_KEY", ""),
            llm=os.environ.get("AGENT_LLM", "scripted"),
            model=os.environ.get("AGENT_MODEL", "claude-opus-5-5"),
            effort=os.environ.get("AGENT_EFFORT", "low"),
            poll_seconds=float(os.environ.get("AGENT_POLL_SECONDS", "2")),
            step_delay_seconds=float(os.environ.get("AGENT_STEP_DELAY_SECONDS", "0")),
            max_tool_turns=int(os.environ.get("AGENT_MAX_TOOL_TURNS", "8")),
            ocr=os.environ.get("AGENT_OCR", "tesseract"),
            ocr_lang=os.environ.get("AGENT_OCR_LANG", "kor+eng"),
            ocr_max_pages=int(os.environ.get("AGENT_OCR_MAX_PAGES", "10")),
        )
