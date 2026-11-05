"""합성 증빙 서류 생성기(PA-13). 평가와 시연용 견본 PNG 를 Pillow 로 그린다.

  python eval/documents.py                       → eval/documents/*.png
  python eval/documents.py --amount 300000 --date 2026-11-05 --out /tmp/docs

서류 내용은 전부 가상이다. 모든 장에 「시연용 견본」을 찍고, 신분증 사본은 만들지 않는다(D37).
기본 금액과 일자는 다툼 거래(이체 300,000원, 2026-11-05)에 맞춘다. forged_receipt 만 금액과 이체 일자가 원장과 어긋난다.
한국어 글꼴이 필요하다: macOS 는 기본 글꼴, Linux 는 `fonts-nanum`. 다른 글꼴은 `EVAL_DOCUMENT_FONT` 로 준다.
"""

from __future__ import annotations

import argparse
import os
from datetime import date, timedelta
from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_CANDIDATES = (
    "/System/Library/Fonts/AppleSDGothicNeo.ttc",
    "/usr/share/fonts/truetype/nanum/NanumGothic.ttf",
    "/usr/share/fonts/nanum/NanumGothic.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
)
WIDTH, HEIGHT = 1240, 1754  # A4, 150dpi
MARGIN = 110
OUT = Path(__file__).parent / "documents"


def find_font() -> str:
    override = os.environ.get("EVAL_DOCUMENT_FONT")
    if override:
        if not Path(override).is_file():
            raise SystemExit(f"EVAL_DOCUMENT_FONT 글꼴 파일이 없습니다: {override}")
        return override
    for candidate in FONT_CANDIDATES:
        if Path(candidate).is_file():
            return candidate
    raise SystemExit(
        "한국어 글꼴을 찾지 못했습니다. macOS 기본 글꼴(AppleSDGothicNeo)이나 Linux `apt install fonts-nanum`, "
        "또는 EVAL_DOCUMENT_FONT=<ttf/ttc 경로>를 지정하세요."
    )


def won(amount: int) -> str:
    return f"{amount:,}원"


def specs(amount: int, day: date) -> dict[str, tuple[str, list[str]]]:
    """파일 이름 → (제목, 본문 줄). 줄 하나가 OCR 영역 하나가 되도록 짧게 쓴다."""
    contract_day = day - timedelta(days=65)
    forged_day = day - timedelta(days=8)
    return {
        "loan_contract": (
            "금전 소비대차 계약서",
            [
                f"계약일: {contract_day.isoformat()}",
                "대여인: 홍길동 (가상)",
                "차용인: 김철수 (가상)",
                f"차용금액: {won(amount * 3)}",
                f"상환방법: 매월 {won(amount)}씩 3회 계좌이체로 상환",
                f"상환기한: {(contract_day + timedelta(days=90)).isoformat()}",
                "연락처: 010-0000-1234",
                "위 내용을 확인하고 서명합니다.",
            ],
        ),
        "transfer_receipt": (
            "이체 확인증",
            [
                f"이체일시: {day.isoformat()} 10:00",
                f"이체금액: {won(amount)}",
                "받는 분: 홍길동 (가상)",
                "보내는 분: 김철수 (가상)",
                "메모: 9월 빌린 돈 상환",
                "처리결과: 정상 처리",
            ],
        ),
        "used_goods_receipt": (
            "중고 물품 거래 영수증",
            [
                f"거래일: {day.isoformat()}",
                "품목: 중고 노트북 1대",
                f"판매금액: {won(amount)}",
                "판매자: 홍길동 (가상)",
                "구매자: 김철수 (가상)",
                "대금은 계좌이체로 받았습니다.",
            ],
        ),
        # 위조 변형: 금액과 이체 일자가 원장과 어긋난다. Agent 는 모순으로, 게이트는 수용을 막아야 한다
        "forged_receipt": (
            "이체 확인증",
            [
                f"이체일시: {forged_day.isoformat()} 09:00",
                f"이체금액: {won(amount * 10)}",
                "받는 분: 홍길동 (가상)",
                "보내는 분: 김철수 (가상)",
                "메모: 물품 대금",
                "처리결과: 정상 처리",
            ],
        ),
    }


def render(title: str, lines: list[str], font_path: str) -> Image.Image:
    image = Image.new("RGB", (WIDTH, HEIGHT), "white")
    draw = ImageDraw.Draw(image)
    stamp = ImageFont.truetype(font_path, 30)
    heading = ImageFont.truetype(font_path, 64)
    body = ImageFont.truetype(font_path, 40)

    draw.text((MARGIN, 60), "시연용 견본 - 실제 서류가 아닙니다", font=stamp, fill=(200, 30, 30))
    draw.text((WIDTH // 2, 200), title, font=heading, fill="black", anchor="mt")
    draw.line((MARGIN, 300, WIDTH - MARGIN, 300), fill="black", width=3)
    y = 360
    for line in lines:
        draw.text((MARGIN, y), line, font=body, fill="black")
        y += 90
    draw.line((MARGIN, y + 20, WIDTH - MARGIN, y + 20), fill=(160, 160, 160), width=2)
    return image


def generate(out: Path = OUT, amount: int = 300_000, day: date = date(2026, 11, 5)) -> list[Path]:
    font_path = find_font()
    out.mkdir(parents=True, exist_ok=True)
    written = []
    for name, (title, lines) in specs(amount, day).items():
        path = out / f"{name}.png"
        render(title, lines, font_path).save(path, "PNG")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="합성 증빙 서류 PNG 생성")
    parser.add_argument("--amount", type=int, default=300_000, help="다툼 거래 금액(원)")
    parser.add_argument("--date", type=date.fromisoformat, default=date(2026, 11, 5), help="다툼 거래 일자")
    parser.add_argument("--out", type=Path, default=OUT)
    args = parser.parse_args()
    for path in generate(args.out, args.amount, args.date):
        print(path)


if __name__ == "__main__":
    main()
