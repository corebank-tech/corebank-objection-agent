"""증빙 서류 글자 인식(PA-12). 로컬 Tesseract CLI 로 읽고, PDF 는 poppler pdftoppm 으로 쪽 이미지로 바꿔 읽는다.

원본 이미지는 LLM 에 보내지 않는다. 워커는 인식한 글자를 서버에 올리고, 서버가 가명 처리한 글자만 분석에 쓴다.
신분증 사본은 서버가 내부 API 로 주지 않고, 워커도 증빙이 아닌 서류는 읽지 않는다(D37).

결과 모양(contract-v2 §6 OCR PUT 본문의 pages):
  [{page, width, height, regions: [{index, text, bbox: [x, y, w, h], confidence}]}]
영역은 줄 단위다. index 는 서류 전체에서 0부터 이어지는 번호라 (documentId, regionIndex)로 영역 하나를 가리킨다.
confidence 는 엔진이 낸 글자 인식 확률(0~1)이다. 0.85 미만은 담당자 확인 대상이다(D40).
"""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Protocol

Page = dict[str, Any]

LOW_CONFIDENCE = 0.85


class OcrError(RuntimeError):
    """서류 하나를 읽지 못했다. 워커는 그 서류만 실패로 남기고 분석을 이어 간다."""


class OcrEngine(Protocol):
    @property
    def name(self) -> str: ...

    def recognize(self, content: bytes) -> list[Page]: ...


def detect_format(content: bytes) -> str:
    """선언된 contentType 이 아니라 매직 바이트로 형식을 정한다(서버 검증과 같은 기준)."""
    if content.startswith(b"%PDF"):
        return "pdf"
    if content.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if content.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    raise OcrError("지원하지 않는 서류 형식")


class TesseractEngine:
    def __init__(
        self, lang: str = "kor+eng", dpi: int = 200, max_pages: int = 10, timeout_seconds: float = 60.0, psm: int = 4
    ):
        self._lang = lang
        # 4 는 「크기가 다른 글이 한 단으로 이어진 문서」. 서식 서류에서 3(자동)보다 줄을 덜 빠뜨린다
        self._psm = psm
        self._dpi = dpi
        self._max_pages = max_pages
        self._timeout = timeout_seconds
        self._name: str | None = None

    @staticmethod
    def available() -> bool:
        return shutil.which("tesseract") is not None

    @property
    def name(self) -> str:
        if self._name is None:
            version = "tesseract"
            try:
                out = self._run([_binary("tesseract"), "--version"])
                version = out.splitlines()[0].strip() or version
            except OcrError:
                pass
            self._name = f"{version} {self._lang}"[:60]
        return self._name

    def recognize(self, content: bytes) -> list[Page]:
        kind = detect_format(content)
        with tempfile.TemporaryDirectory(prefix="ocr-") as tmp:
            work = Path(tmp)
            if kind == "pdf":
                images = self._pdf_pages(content, work)
            else:
                image = work / f"page.{kind}"
                image.write_bytes(content)
                images = [image]
            pages: list[Page] = []
            next_index = 0
            for number, image in enumerate(images, start=1):
                # 한 번 돌려 txt 와 tsv 를 함께 받는다. tsv 는 한글 음절을 단어로 쪼개는 일이 있어 줄 글은 txt 에서 가져온다
                base = work / f"out-{number}"
                self._run(
                    [
                        _binary("tesseract"),
                        str(image),
                        str(base),
                        "-l",
                        self._lang,
                        "--psm",
                        str(self._psm),
                        "txt",
                        "tsv",
                    ]
                )
                tsv = base.with_suffix(".tsv").read_text("utf-8", "replace")
                text = base.with_suffix(".txt").read_text("utf-8", "replace")
                page = parse_tsv(tsv, number, next_index, text)
                next_index += len(page["regions"])
                pages.append(page)
        return pages

    def _pdf_pages(self, content: bytes, work: Path) -> list[Path]:
        source = work / "document.pdf"
        source.write_bytes(content)
        prefix = work / "page"
        self._run(
            [_binary("pdftoppm"), "-r", str(self._dpi), "-png", "-l", str(self._max_pages), str(source), str(prefix)]
        )
        # pdftoppm 은 쪽 수에 따라 page-1.png 또는 page-01.png 로 이름을 붙인다
        images = sorted(work.glob("page-*.png"), key=lambda p: int(p.stem.rsplit("-", 1)[1]))
        if not images:
            raise OcrError("PDF 에서 쪽 이미지를 만들지 못함")
        return images

    def _run(self, args: list[str]) -> str:
        try:
            # 인자는 목록으로만 넘기고 셸을 쓰지 않는다. 파일 경로는 워커가 만든 임시 경로다
            result = subprocess.run(  # noqa: S603
                args, capture_output=True, timeout=self._timeout, check=False
            )
        except subprocess.TimeoutExpired as exc:
            raise OcrError(f"{Path(args[0]).name} 시간 초과") from exc
        if result.returncode != 0:
            message = result.stderr.decode("utf-8", "replace").strip().splitlines()
            raise OcrError(f"{Path(args[0]).name} 실패: {message[-1] if message else result.returncode}"[:200])
        return result.stdout.decode("utf-8", "replace")


def _binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise OcrError(f"{name} 가 설치돼 있지 않음")
    return path


def parse_tsv(tsv: str, page_number: int, start_index: int = 0, text: str | None = None) -> Page:
    """Tesseract TSV 를 줄 단위 영역으로 묶는다. 단어(level 5)를 (block, par, line)으로 모아 상자를 합치고 신뢰도를 평균한다.

    text 는 같은 실행의 txt 출력이다. 공백을 뺀 글자가 같은 줄이 있으면 그 줄의 띄어쓰기를 쓰고, 없으면 단어를 공백으로 잇는다.
    """
    spaced: dict[str, list[str]] = {}
    for line in (text or "").splitlines():
        if line.strip():
            spaced.setdefault(_compact(line), []).append(line.strip())
    width = height = 0
    lines: dict[tuple[int, int, int], list[tuple[str, int, int, int, int, float]]] = {}
    rows = tsv.splitlines()
    for row in rows[1:]:
        cols = row.split("\t")
        if len(cols) < 12:
            continue
        level = int(cols[0])
        left, top, w, h = (int(c) for c in cols[6:10])
        if level == 1:
            width, height = w, h
            continue
        if level != 5:
            continue
        text = cols[11].strip()
        conf = float(cols[10])
        if not text or conf < 0:
            continue
        key = (int(cols[2]), int(cols[3]), int(cols[4]))
        lines.setdefault(key, []).append((text, left, top, w, h, conf))

    regions = []
    for words in lines.values():
        x0 = min(w[1] for w in words)
        y0 = min(w[2] for w in words)
        x1 = max(w[1] + w[3] for w in words)
        y1 = max(w[2] + w[4] for w in words)
        joined = " ".join(w[0] for w in words)
        candidates = spaced.get(_compact(joined))
        regions.append(
            {
                "index": start_index + len(regions),
                "text": candidates.pop(0) if candidates else joined,
                "bbox": [x0, y0, x1 - x0, y1 - y0],
                "confidence": round(sum(w[5] for w in words) / len(words) / 100, 3),
            }
        )
    return {"page": page_number, "width": width, "height": height, "regions": regions}


def _compact(value: str) -> str:
    return "".join(value.split())


def count_regions(pages: list[Page]) -> int:
    return sum(len(p.get("regions") or []) for p in pages)


def count_low_confidence(pages: list[Page]) -> int:
    return sum(1 for p in pages for r in p.get("regions") or [] if (r.get("confidence") or 0) < LOW_CONFIDENCE)
