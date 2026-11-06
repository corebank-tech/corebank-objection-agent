"""OCR 전처리. 좌표 되돌리기, 기울기 추정, 조명 평탄화, 실제 Tesseract 로 돌아간 서류 읽기."""

import io
import shutil
import sys
from datetime import date
from pathlib import Path

import pytest
from PIL import Image, ImageDraw

from objection_agent import preprocess
from objection_agent.ocr import TesseractEngine

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "eval"))


def dot_image(size=(800, 600), at=(200, 150)) -> Image.Image:
    image = Image.new("L", size, 255)
    ImageDraw.Draw(image).rectangle([at[0] - 3, at[1] - 3, at[0] + 3, at[1] + 3], fill=0)
    return image


def dot_center(image: Image.Image) -> tuple[float, float]:
    xs, ys = [], []
    px = image.load()
    for y in range(image.height):
        for x in range(image.width):
            if px[x, y] < 128:
                xs.append(x)
                ys.append(y)
    return sum(xs) / len(xs), sum(ys) / len(ys)


@pytest.mark.parametrize("angle", [3.0, -8.0, 90.0, -90.0, 180.0])
def test_rotated_box_maps_back_to_original(angle):
    original = dot_image()
    transform = preprocess.Transform(original.size)
    rotated = preprocess.rotate(original, angle, transform)
    cx, cy = dot_center(rotated)

    x, y, w, h = transform.to_original([round(cx) - 2, round(cy) - 2, 4, 4])

    assert abs(x + w / 2 - 200) <= 3
    assert abs(y + h / 2 - 150) <= 3


def test_scale_then_rotate_maps_back():
    original = dot_image(size=(400, 300), at=(100, 80))
    transform = preprocess.Transform(original.size)
    scaled = preprocess.upscale(original, transform)
    rotated = preprocess.rotate(scaled, -5.0, transform)
    cx, cy = dot_center(rotated)

    x, y, w, h = transform.to_original([round(cx) - 4, round(cy) - 4, 8, 8])

    assert abs(x + w / 2 - 100) <= 3
    assert abs(y + h / 2 - 80) <= 3


def test_box_is_clamped_to_original_bounds():
    transform = preprocess.Transform((100, 100))
    assert transform.to_original([-20, -20, 300, 300]) == [0, 0, 100, 100]


def lines_image(angle: float) -> Image.Image:
    image = Image.new("L", (1200, 900), 255)
    draw = ImageDraw.Draw(image)
    for y in range(120, 820, 70):
        draw.rectangle([150, y, 1050, y + 18], fill=0)
    return image.rotate(angle, expand=True, fillcolor=255)


@pytest.mark.parametrize("angle", [0.0, 2.5, -6.0, 10.0])
def test_skew_is_estimated(angle):
    assert abs(preprocess.estimate_skew(lines_image(angle)) + angle) <= 0.6


def test_flatten_turns_gray_paper_white_and_keeps_text_dark():
    gray = Image.new("L", (600, 400), 170)
    ImageDraw.Draw(gray).rectangle([100, 180, 500, 200], fill=40)

    flat = preprocess.flatten(gray)

    assert flat.getpixel((50, 50)) >= 240
    assert flat.getpixel((300, 190)) <= 60


def test_exif_orientation_is_applied():
    image = Image.new("RGB", (300, 100), "white")
    exif = Image.Exif()
    exif[0x0112] = 6  # 오른쪽으로 90도 돌려 보라는 태그
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", exif=exif)

    gray, transform = preprocess.load_gray(buffer.getvalue())

    assert gray.size == (100, 300)
    assert transform.original_size == (100, 300)


needs_tesseract = pytest.mark.skipif(shutil.which("tesseract") is None, reason="tesseract 가 없는 환경")


@needs_tesseract
def test_real_tesseract_reads_document_turned_90_degrees():
    documents = pytest.importorskip("documents")
    try:
        font = documents.find_font()
    except Exception:
        pytest.skip("한글 글꼴이 없는 환경")
    title, lines = documents.specs(300_000, date(2026, 11, 5))["loan_contract"]
    image = documents.render(title, lines, font).rotate(90, expand=True)
    buffer = io.BytesIO()
    image.save(buffer, "PNG")

    pages = TesseractEngine().recognize(buffer.getvalue())

    text = "".join(r["text"] for r in pages[0]["regions"]).replace(" ", "")
    assert "900,000원" in text
    assert "2026-09-01" in text
    # 영역 상자는 원본(돌아간 이미지) 좌표 안에 있다
    assert (pages[0]["width"], pages[0]["height"]) == image.size
    for region in pages[0]["regions"]:
        x, y, w, h = region["bbox"]
        assert x >= 0 and y >= 0 and x + w <= image.width and y + h <= image.height
