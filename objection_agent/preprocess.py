"""OCR 전처리. 휴대폰으로 찍은 서류에서 흔한 회전, 기울기, 고르지 않은 조명, 잡음, 낮은 해상도를 바로잡는다.

전처리한 이미지 좌표로 읽은 영역 상자는 담당자 화면이 원본 서류 위에 그리므로, 적용한 변환을 기록해 두고 원본 좌표로
되돌린다(`Transform.to_original`).

측정(eval/ocr_bench.py): 8도 기울기, 90도 회전, 3% 잡음, 사진 흉내에서 글자 오류율이 크게 줄고 깨끗한 서류는 그대로다.
"""

from __future__ import annotations

import io
import math
from dataclasses import dataclass, field

from PIL import Image, ImageFilter, ImageMath, ImageOps

# 이보다 좁은 이미지는 키워서 읽는다. Tesseract 는 글자 높이 20~30픽셀 이상에서 안정적이다
TARGET_WIDTH = 1600
# 키우는 배율과 결과 픽셀 수의 상한. 좁고 긴 이미지(예: 20x50000)를 키우다 메모리가 바닥나는 것을 막는다
MAX_SCALE = 4.0
MAX_PIXELS = 40_000_000
# 이보다 길쭉한 이미지는 서류로 보지 않는다
MAX_ASPECT = 20.0
# 기울기 탐색 범위(도). 이보다 크게 기운 서류는 방향 감지(90도 단위)가 맡는다
MAX_SKEW = 12.0
# 이보다 작은 기울기는 고치지 않는다. 회전 보간이 오히려 글자를 흐린다
MIN_SKEW = 0.3


@dataclass
class Transform:
    """원본 좌표 → 전처리 좌표 변환 기록. 역순으로 되돌린다."""

    original_size: tuple[int, int]
    ops: list[tuple] = field(default_factory=list)

    def copy(self) -> Transform:
        return Transform(self.original_size, list(self.ops))

    def to_original(self, bbox: list[int]) -> list[int]:
        x, y, w, h = bbox
        corners = [(x, y), (x + w, y), (x, y + h), (x + w, y + h)]
        for op in reversed(self.ops):
            if op[0] == "scale":
                factor = op[1]
                corners = [(px / factor, py / factor) for px, py in corners]
            else:
                _, angle, before, after = op
                corners = [_unrotate(px, py, angle, before, after) for px, py in corners]
        ow, oh = self.original_size
        xs = [min(max(px, 0), ow) for px, _ in corners]
        ys = [min(max(py, 0), oh) for _, py in corners]
        x0, y0 = math.floor(min(xs)), math.floor(min(ys))
        return [x0, y0, math.ceil(max(xs)) - x0, math.ceil(max(ys)) - y0]


def _unrotate(
    px: float, py: float, angle: float, before: tuple[int, int], after: tuple[int, int]
) -> tuple[float, float]:
    # Pillow 의 rotate(angle) 는 화면 기준 반시계 방향이다. 이미지 좌표(y 아래)에서 정방향은
    # x' = cos*dx + sin*dy, y' = -sin*dx + cos*dy 이므로 역변환은 아래와 같다
    rad = math.radians(angle)
    cos, sin = math.cos(rad), math.sin(rad)
    dx, dy = px - after[0] / 2, py - after[1] / 2
    return before[0] / 2 + cos * dx - sin * dy, before[1] / 2 + sin * dx + cos * dy


def load_gray(content: bytes) -> tuple[Image.Image, Transform]:
    image = Image.open(io.BytesIO(content))
    image.load()
    image = ImageOps.exif_transpose(image)  # 휴대폰 사진의 EXIF 방향 태그를 먼저 반영한다
    gray = image.convert("L")
    return gray, Transform(gray.size)


class ImageRejected(ValueError):
    """서류로 읽을 수 없는 크기나 비율이다."""


def upscale(image: Image.Image, transform: Transform) -> Image.Image:
    """좁은 이미지는 키우고, 너무 큰 이미지는 줄인다. 결과는 항상 MAX_PIXELS 이하다."""
    w, h = image.size
    if max(w, h) / max(min(w, h), 1) > MAX_ASPECT:
        raise ImageRejected("가로세로 비율이 서류 범위를 벗어남")
    factor = min(TARGET_WIDTH / w, MAX_SCALE) if w < TARGET_WIDTH else 1.0
    factor = min(factor, math.sqrt(MAX_PIXELS / (w * h)))
    if abs(factor - 1.0) < 1e-6:
        return image
    transform.ops.append(("scale", factor))
    return image.resize((max(1, round(w * factor)), max(1, round(h * factor))), Image.LANCZOS)


def flatten(image: Image.Image) -> Image.Image:
    """조명 평탄화. 글자를 지운 배경(최댓값 필터 + 흐림)으로 나눠 바탕을 흰색으로 편다.

    이 단계가 회전보다 먼저여야 한다. 회색 종이를 돌리면 모서리에 흰 삼각형이 생기고, Tesseract 의 이진화가 회색 종이 전체를
    글자로 잡아 아무것도 읽지 못한다.
    """
    background = image.filter(ImageFilter.MaxFilter(15)).filter(ImageFilter.GaussianBlur(20))
    flat = ImageMath.lambda_eval(
        lambda a: a["convert"](a["min"](a["img"] * 255 / (a["bg"] + 1), 255), "L"), img=image, bg=background
    )
    return ImageOps.autocontrast(flat, cutoff=1)


def rotate(image: Image.Image, angle: float, transform: Transform) -> Image.Image:
    rotated = image.rotate(angle, expand=True, fillcolor=255, resample=Image.BICUBIC)
    transform.ops.append(("rotate", angle, image.size, rotated.size))
    return rotated


def estimate_skew(image: Image.Image) -> float:
    """줄 방향 투영의 분산이 가장 큰 각도. 글자 줄이 수평일 때 행 평균이 줄과 줄 사이에서 크게 출렁인다."""
    small = image.copy()
    small.thumbnail((900, 900))
    small = small.point(lambda v: 0 if v < 180 else 255)

    def score(angle: float) -> float:
        rows = small.rotate(angle, fillcolor=255, resample=Image.BILINEAR).resize((1, small.height), Image.BOX)
        values = list(rows.get_flattened_data())
        mean = sum(values) / len(values)
        return sum((v - mean) ** 2 for v in values)

    coarse = max((a / 2 for a in range(int(-MAX_SKEW * 2), int(MAX_SKEW * 2) + 1)), key=score)
    return max((coarse + d / 10 for d in range(-5, 6)), key=score)


def deskew(image: Image.Image, transform: Transform) -> Image.Image:
    angle = estimate_skew(image)
    return rotate(image, angle, transform) if abs(angle) >= MIN_SKEW else image


def denoise(image: Image.Image) -> Image.Image:
    return image.filter(ImageFilter.MedianFilter(3))


def to_png(image: Image.Image) -> bytes:
    buffer = io.BytesIO()
    image.save(buffer, "PNG")
    return buffer.getvalue()
