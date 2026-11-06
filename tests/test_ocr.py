"""OCR 결과 모양과 Tesseract 연동. 실제 tesseract 시험은 바이너리와 한국어 데이터, 한국어 글꼴이 있을 때만 돈다."""

from __future__ import annotations

import io
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from objection_agent import ocr
from objection_agent.llm.scripted import ScriptedAnalyzer
from objection_agent.ocr import OcrError, TesseractEngine, detect_format, parse_tsv
from objection_agent.server import ServerClient
from objection_agent.worker import run_once
from tests.fake_server import FakeServer, document, relationship_ledger

HEADER = "level\tpage_num\tblock_num\tpar_num\tline_num\tword_num\tleft\ttop\twidth\theight\tconf\ttext"
TSV = "\n".join(
    [
        HEADER,
        "1\t1\t0\t0\t0\t0\t0\t0\t1240\t1754\t-1\t",
        "4\t1\t1\t1\t1\t0\t100\t50\t300\t40\t-1\t",
        "5\t1\t1\t1\t1\t1\t100\t50\t20\t40\t90\t이",
        "5\t1\t1\t1\t1\t2\t122\t50\t60\t40\t80\t체금액:",
        "5\t1\t1\t1\t1\t3\t200\t52\t200\t38\t70\t300,000원",
        "5\t1\t1\t1\t2\t1\t100\t120\t80\t40\t95\t받는",
        "5\t1\t1\t1\t2\t2\t190\t121\t40\t40\t-1\t ",
        "5\t1\t1\t1\t2\t3\t240\t120\t60\t42\t85\t분",
    ]
)


def test_parse_tsv_groups_words_into_lines():
    page = parse_tsv(TSV, 2, start_index=7, text="이체금액: 300,000원\n\n받는 분\n")

    assert page["page"] == 2 and page["width"] == 1240 and page["height"] == 1754
    first, second = page["regions"]
    # 단어 상자를 합치고, 띄어쓰기는 txt 출력에서 가져온다. 번호는 서류 전체에서 이어진다
    assert first == {"index": 7, "text": "이체금액: 300,000원", "bbox": [100, 50, 300, 40], "confidence": 0.8}
    assert second["index"] == 8 and second["text"] == "받는 분"
    assert second["bbox"] == [100, 120, 200, 42]
    assert second["confidence"] == 0.9  # 신뢰도 -1(빈 단어)은 뺀다


def test_parse_tsv_without_text_joins_words():
    assert parse_tsv(TSV, 1)["regions"][0]["text"] == "이 체금액: 300,000원"


def test_detect_format_uses_magic_bytes():
    assert detect_format(b"%PDF-1.7") == "pdf"
    assert detect_format(b"\x89PNG\r\n\x1a\nxxxx") == "png"
    assert detect_format(b"\xff\xd8\xff\xe0") == "jpeg"
    with pytest.raises(OcrError):
        detect_format(b"GIF89a")


def _small_png() -> bytes:
    import io

    from PIL import Image

    buffer = io.BytesIO()
    Image.new("L", (40, 20), 255).save(buffer, "PNG")
    return buffer.getvalue()


def test_missing_binary_is_ocr_error(monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda _: None)
    with pytest.raises(OcrError, match="설치"):
        TesseractEngine().recognize(_small_png())


def test_broken_image_is_ocr_error():
    with pytest.raises(OcrError, match="이미지"):
        TesseractEngine().recognize(b"\x89PNG\r\n\x1a\nxxxx")


def _korean_tesseract() -> bool:
    if not TesseractEngine.available():
        return False
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True, check=False)  # noqa: S607
    return "kor" in out.stdout.split()


def _documents_module():
    pytest.importorskip("PIL")
    sys.path.insert(0, str(Path(__file__).parent.parent / "eval"))
    try:
        import documents
    finally:
        sys.path.pop(0)
    try:
        documents.find_font()
    except SystemExit:
        pytest.skip("한국어 글꼴 없음")
    return documents


requires_tesseract = pytest.mark.skipif(not _korean_tesseract(), reason="tesseract 와 kor 데이터 없음")


@requires_tesseract
def test_real_tesseract_reads_generated_receipt(tmp_path):
    documents = _documents_module()
    paths = {p.stem: p for p in documents.generate(tmp_path)}

    pages = TesseractEngine().recognize(paths["transfer_receipt"].read_bytes())

    assert len(pages) == 1 and pages[0]["width"] == documents.WIDTH
    texts = [r["text"] for r in pages[0]["regions"]]
    assert any("300,000" in t for t in texts)
    assert any("2026-11-05" in t for t in texts)
    for region in pages[0]["regions"]:
        assert 0 <= region["confidence"] <= 1
        assert len(region["bbox"]) == 4


@requires_tesseract
@pytest.mark.skipif(shutil.which("pdftoppm") is None, reason="pdftoppm 없음")
def test_real_tesseract_reads_pdf_pages_with_continuous_index(tmp_path):
    documents = _documents_module()
    from PIL import Image

    paths = {p.stem: p for p in documents.generate(tmp_path)}
    images = [Image.open(paths["transfer_receipt"]), Image.open(paths["used_goods_receipt"])]
    buffer = io.BytesIO()
    images[0].save(buffer, "PDF", save_all=True, append_images=images[1:], resolution=150)

    pages = TesseractEngine().recognize(buffer.getvalue())

    assert [p["page"] for p in pages] == [1, 2]
    indexes = [r["index"] for p in pages for r in p["regions"]]
    assert indexes == list(range(len(indexes)))


@requires_tesseract
def test_forged_receipt_end_to_end_with_real_tesseract(tmp_path):
    documents = _documents_module()
    paths = {p.stem: p for p in documents.generate(tmp_path)}
    fake = FakeServer(
        relationship_ledger(),
        "친구에게 빌려준 돈을 돌려받았습니다.",
        documents=[document(5, paths["forged_receipt"].read_bytes())],
    )

    run_once(
        ServerClient("http://core/api/v1", "key", transport=fake.transport()),
        ScriptedAnalyzer(),
        ocr=ocr.TesseractEngine(),
    )

    assert fake.steps[2]["type"] == "OCR" and fake.steps[2]["detail"]["regions"] > 0
    draft = fake.drafts[0]
    assert draft["branch"] == "UNDETERMINED"
    assert any("3,000,000원" in c["ledgerFact"] for c in draft["contradictions"])
    assert any("2026-10-28" in c["ledgerFact"] for c in draft["contradictions"])
