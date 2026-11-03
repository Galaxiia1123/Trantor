import hashlib
import io

import pytest
from PIL import Image, ImageDraw, ImageFont
from fastapi.testclient import TestClient

from chuantuo.intake import MaterialStore, recognize
from chuantuo.web import create_app


def text_pdf():
    # A minimal text-bearing fixture; no external document or model is needed.
    content = b"BT /F1 18 Tf 40 150 Td (Harbor plan: city and company negotiate access.) Tj ET"
    objects = [b"<< /Type /Catalog /Pages 2 0 R >>",
               b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
               b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 620 220] /Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
               b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
               b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
    result = b"%PDF-1.4\n"
    offsets = [0]
    for i, obj in enumerate(objects, 1):
        offsets.append(len(result)); result += f"{i} 0 obj\n".encode() + obj + b"\nendobj\n"
    start = len(result)
    result += b"xref\n0 6\n0000000000 65535 f \n"
    for offset in offsets[1:]: result += f"{offset:010d} 00000 n \n".encode()
    return result + f"trailer\n<< /Size 6 /Root 1 0 R >>\nstartxref\n{start}\n%%EOF".encode()


def picture_bytes(fmt="PNG"):
    image = Image.new("RGB", (1000, 200), "white")
    try:
        font = ImageFont.truetype("C:/Windows/Fonts/arial.ttf", 38)
    except OSError:
        font = ImageFont.load_default(size=38)
    ImageDraw.Draw(image).text((30, 65), "HARBOR PLAN 2026", fill="black", font=font)
    output = io.BytesIO(); image.save(output, format=fmt)
    return image, output.getvalue()


def test_original_text_and_pdf_pages_are_preserved(tmp_path):
    store = MaterialStore(tmp_path, ocr=lambda image: pytest.fail("Text PDF should not need OCR"))
    raw = "  港口原文\n不应改写\n".encode("utf-8")
    source = store.ingest("notes.md", raw)
    assert source.text == raw.decode("utf-8")
    assert source.sha256 == hashlib.sha256(raw).hexdigest()
    assert (store.load(source.asset_id)[1] / "original").read_bytes() == raw
    pdf = store.ingest("report.pdf", text_pdf())
    assert "city and company" in pdf.text
    assert pdf.pages[0].method == "pdf_text"
    assert pdf.pages[0].number == 1
    assert store.images([pdf])[0]["page"] == 1


def test_image_and_scanned_pdf_use_ocr_with_traceable_pages(tmp_path):
    store = MaterialStore(tmp_path, ocr=lambda image: ("工程师提出现场建议", .72))
    _, raw = picture_bytes()
    image = store.ingest("chart.png", raw)
    assert image.text == "工程师提出现场建议"
    assert image.pages[0].method == "ocr" and image.pages[0].confidence == .72
    assert any("置信度" in warning for warning in image.warnings)
    _, pdf = picture_bytes("PDF")
    scanned = store.ingest("scan.pdf", pdf)
    assert scanned.pages[0].method == "ocr"
    assert "第 1 页" in scanned.text
    assert store.images([scanned])[0]["data_url"].startswith("data:image/jpeg;base64,")


def test_real_local_ocr_recognizes_synthetic_image():
    image, _ = picture_bytes()
    text, confidence = recognize(image)
    assert "HARBOR" in text.upper() and "2026" in text
    assert confidence > .7


def test_upload_download_no_model_calls_and_invalid_input(tmp_path):
    class NoModel:
        def complete(self, *args): pytest.fail("Intake must not call a paid model")
    client = TestClient(create_app(tmp_path, gateway=NoModel()))
    response = client.post("/api/materials", files={"file": ("source.txt", "港口材料".encode(), "text/plain")})
    assert response.status_code == 200
    source = response.json()["source"]
    assert client.get(f"/api/materials/{source['asset_id']}/original").content == "港口材料".encode()
    assert client.post("/api/materials", files={"file": ("bad.pdf", b"not a PDF")}).status_code == 400
    assert client.post("/api/materials", files={"file": ("script.exe", b"content")}).status_code == 400

