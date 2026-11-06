"""OCR 품질 측정. 정답 글을 아는 합성 서류에 실무에서 흔한 열화를 주고 글자 오류율과 핵심 항목 추출을 잰다.

  .venv/bin/python eval/ocr_bench.py            → eval/ocr_bench_result.md 와 표준 출력

글자 오류율(CER) = 정답 줄과 가장 가까운 인식 줄 사이 편집 거리 합 / 정답 글자 수(공백 제외).
핵심 항목 = 금액(n,nnn원), 날짜(YYYY-MM-DD), 연락처 형태가 인식 결과에 그대로 있는 비율.
"""

from __future__ import annotations

import io
import random
import re
import sys
from datetime import date
from pathlib import Path

from PIL import Image, ImageEnhance, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import documents  # noqa: E402

from objection_agent.ocr import LOW_CONFIDENCE, TesseractEngine  # noqa: E402

FIELD = re.compile(r"\d{1,3}(?:,\d{3})+원|\d{4}-\d{2}-\d{2}|010-\d{4}-\d{4}")
SERIF_CANDIDATES = [
    "/System/Library/Fonts/Supplemental/AppleMyungjo.ttf",
    "/usr/share/fonts/truetype/nanum/NanumMyeongjo.ttf",
]


def compact(text: str) -> str:
    return re.sub(r"\s+", "", text)


def distance(a: str, b: str) -> int:
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def to_png(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.save(buf, "PNG")
    return buf.getvalue()


def to_jpeg(image: Image.Image, quality: int) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def to_pdf(image: Image.Image) -> bytes:
    buf = io.BytesIO()
    image.convert("RGB").save(buf, "PDF", resolution=200)
    return buf.getvalue()


def noise(image: Image.Image, ratio: float, seed: int = 7) -> Image.Image:
    rng = random.Random(seed)  # noqa: S311 - 측정용 잡음. 보안 용도가 아니다
    out = image.convert("L").copy()
    px = out.load()
    w, h = out.size
    for _ in range(int(w * h * ratio)):
        px[rng.randrange(w), rng.randrange(h)] = rng.choice((0, 255))
    return out


def photo(image: Image.Image) -> bytes:
    """휴대폰으로 찍은 서류 흉내: 약간 기울고, 흐리고, 어둡고, 고르지 않은 조명, JPEG 압축."""
    tilted = image.rotate(2.5, expand=True, fillcolor="white", resample=Image.BICUBIC)
    blurred = tilted.filter(ImageFilter.GaussianBlur(1.0))
    darker = ImageEnhance.Brightness(blurred).enhance(0.8)
    w, h = darker.size
    shade = Image.linear_gradient("L").resize((w, h)).point(lambda v: 255 - v // 4)
    lit = Image.composite(darker, Image.new("RGB", (w, h), (120, 120, 120)), shade)
    small = lit.resize((w * 2 // 3, h * 2 // 3), Image.LANCZOS)
    return to_jpeg(small, 55)


def variants(image: Image.Image) -> dict[str, bytes]:
    w, h = image.size
    return {
        "원본 PNG": to_png(image),
        "PDF": to_pdf(image),
        "저해상도 50%": to_png(image.resize((w // 2, h // 2), Image.LANCZOS)),
        "저해상도 33%": to_png(image.resize((w // 3, h // 3), Image.LANCZOS)),
        "기울기 3도": to_png(image.rotate(3, expand=True, fillcolor="white", resample=Image.BICUBIC)),
        "기울기 8도": to_png(image.rotate(8, expand=True, fillcolor="white", resample=Image.BICUBIC)),
        "흐림": to_png(image.filter(ImageFilter.GaussianBlur(2.0))),
        "JPEG 품질 25": to_jpeg(image, 25),
        "잡음 3%": to_png(noise(image, 0.03)),
        "휴대폰 촬영 흉내": photo(image),
        "90도 회전": to_png(image.rotate(90, expand=True)),
    }


def score(expected: list[str], pages: list[dict]) -> tuple[int, int, int, int]:
    got = [compact(r["text"]) for p in pages for r in p["regions"]]
    full = "".join(got)
    errors = total = 0
    for line in expected:
        target = compact(line)
        best = min((distance(target, g) for g in got), default=len(target))
        errors += min(best, len(target))
        total += len(target)
    fields = [f for line in expected for f in FIELD.findall(line)]
    hit = sum(1 for f in fields if compact(f) in full)
    return errors, total, hit, len(fields)


def run(engine: TesseractEngine, fonts: dict[str, str]) -> dict[str, list]:
    rows: dict[str, list] = {}
    for font_name, font in fonts.items():
        for title, lines in documents.specs(300_000, date(2026, 11, 5)).values():
            image = documents.render(title, lines, font)
            for variant, content in variants(image).items():
                key = f"{font_name}|{variant}"
                try:
                    pages = engine.recognize(content)
                    e, t, hit, n = score([title, *lines], pages)
                    low = sum(1 for p in pages for r in p["regions"] if r["confidence"] < LOW_CONFIDENCE)
                    rows.setdefault(key, []).append((e, t, hit, n, low, 0))
                except Exception:  # 측정 도구라 실패도 결과로 남긴다
                    rows.setdefault(key, []).append((0, 0, 0, 0, 0, 1))
    return rows


def summary(group: list) -> tuple[str, str]:
    e = sum(r[0] for r in group)
    t = sum(r[1] for r in group) or 1
    hit = sum(r[2] for r in group)
    n = sum(r[3] for r in group) or 1
    fail = sum(r[5] for r in group)
    return f"{e / t * 100:.1f}%", f"{hit}/{n}" + (f", 실패 {fail}" if fail else "")


def main() -> None:
    regular = documents.find_font()
    serif = next((p for p in SERIF_CANDIDATES if Path(p).exists()), None)
    fonts = {"고딕": regular} | ({"명조": serif} if serif else {})
    raw = run(TesseractEngine(preprocess=False), fonts)
    fixed = run(TesseractEngine(), fonts)
    total_raw = [r for group in raw.values() for r in group]
    total_fixed = [r for group in fixed.values() for r in group]
    out = [
        "# OCR 품질 측정 (합성 서류)",
        "",
        f"엔진 {TesseractEngine().name}. 서류 {len(documents.specs(1, date.today()))}종 × 글꼴 {len(fonts)}종 × 열화 11종.",
        "글자 오류율은 정답 줄과 가장 가까운 인식 줄의 편집 거리 합 / 정답 글자 수(공백 제외). 핵심 항목은 금액, 날짜, 연락처가 그대로 읽힌 수.",
        "",
        f"전체: 보정 전 글자 오류율 {summary(total_raw)[0]}, 핵심 항목 {summary(total_raw)[1]} → 보정 후 {summary(total_fixed)[0]}, {summary(total_fixed)[1]}",
        "",
        "| 글꼴 | 열화 | 글자 오류율 보정 전 | 보정 후 | 핵심 항목 보정 전 | 보정 후 |",
        "|---|---|---|---|---|---|",
    ]
    for key in raw:
        font, variant = key.split("|")
        a, b = summary(raw[key]), summary(fixed[key])
        out.append(f"| {font} | {variant} | {a[0]} | {b[0]} | {a[1]} | {b[1]} |")
    text = "\n".join(out) + "\n"
    (Path(__file__).parent / "ocr_bench_result.md").write_text(text)
    print(text)


if __name__ == "__main__":
    main()
