"""Local unstructured material intake. Originals stay on disk, never in agents."""
from __future__ import annotations

import base64
import hashlib
import io
import re
import threading
import uuid
from pathlib import Path
from tempfile import TemporaryDirectory
from xml.etree import ElementTree
from zipfile import BadZipFile, ZipFile

import numpy as np
import pypdfium2 as pdfium
from PIL import Image, ImageOps, UnidentifiedImageError

from .schema import Source, SourcePage
from .models import protect_credentials

MAX_FILE_BYTES = 32 * 1024 * 1024
MAX_PDF_PAGES = 100
_pdf_lock = threading.Lock()  # PDFium does not permit simultaneous thread calls.
_ocr_lock = threading.Lock()
_ocr_engine = None


def docx_body(raw):
    with io.BytesIO(raw) as source:
        try:
            with ZipFile(source) as archive:
                item = archive.getinfo("word/document.xml")
                if item.file_size > MAX_FILE_BYTES or item.flag_bits & 1:
                    raise ValueError("Word 正文超出限额或已加密")
                content = archive.read(item)
            if b"<!DOCTYPE" in content.replace(b"\x00", b"").upper():
                raise ValueError("Word 正文包含不支持的文档类型声明")
            root = ElementTree.fromstring(content)
        except (BadZipFile, KeyError, ElementTree.ParseError, NotImplementedError):
            raise ValueError("无法读取 DOCX 正文，请使用有效的 Word 文档") from None
    namespace = root.tag.rpartition("}")[0] + "}"
    paragraphs = []
    # Word has no reliable physical page numbers without a layout engine.
    # Walk paragraphs once, including table cells, and keep text, tabs and explicit breaks.
    def visit(node, parts=None):
        if node.tag == namespace + "p":
            inner = []
            for child in node:
                visit(child, inner)
            if inner:
                paragraphs.append("".join(inner))
            return
        if parts is not None and node.tag in {namespace+"t", namespace+"tab", namespace+"br", namespace+"cr"}:
            parts.append(node.text or "" if node.tag == namespace+"t" else "\t" if node.tag == namespace+"tab" else "\n")
            return
        if node.tag not in {namespace+"del",namespace+"drawing",namespace+"pict"}:
            for child in node:
                visit(child, parts)
    try:
        visit(root)
    except RecursionError:
        raise ValueError("Word 正文嵌套过深，无法读取") from None
    return "\n\n".join(paragraphs)


def recognize(image: Image.Image) -> tuple[str, float | None]:
    global _ocr_engine
    from rapidocr_onnxruntime import RapidOCR

    with _ocr_lock:
        if _ocr_engine is None:
            _ocr_engine = RapidOCR(intra_op_num_threads=2, inter_op_num_threads=1)
        result, _ = _ocr_engine(np.asarray(image.convert("RGB")))
    if not result:
        return "", None
    return "\n".join(line[1] for line in result), sum(float(line[2]) for line in result) / len(result)


class MaterialStore:
    def __init__(self, directory: Path, ocr=None):
        self.directory = Path(directory)
        self.ocr = ocr or recognize

    def ingest(self, filename: str, raw: bytes, progress=None, *, asset_id=None, credential_configs=()) -> Source:
        progress = progress or (lambda stage, message: None)
        name = filename.replace("\\", "/").rsplit("/", 1)[-1]
        suffix = Path(name).suffix.lower()
        if not raw or len(raw) > MAX_FILE_BYTES:
            raise ValueError("材料不能为空，单文件上限为 32 MiB。")
        if suffix not in {".txt", ".md", ".markdown", ".docx", ".pdf", ".png", ".jpg", ".jpeg", ".webp"}:
            raise ValueError("支持 PDF、DOCX、PNG、JPEG、WebP、TXT 和 Markdown 材料。")
        asset_id = asset_id or uuid.uuid4().hex
        if not re.fullmatch(r"[a-f0-9]{32}",asset_id):
            raise ValueError("材料编号无效")
        folder = self.directory / asset_id
        texts, pages, warnings, previews = [], [], [], []
        offset = 0

        def add_page(number, text, method, confidence=None):
            nonlocal offset
            block = f"【第 {number} 页】\n{text}" if suffix == ".pdf" else text
            if texts:
                offset += 2
            pages.append(SourcePage(number=number, method=method, text_start=offset,
                                    text_end=offset + len(block), confidence=confidence))
            texts.append(block)
            progress("extracting", f"已读取「{name}」第 {number} 页")
            offset += len(block)
            if not text.strip():
                warnings.append(f"第 {number} 页未识别到文字；可能包含图形、空白或难以识别的内容。")
            elif confidence is not None and confidence < 0.8:
                warnings.append(f"第 {number} 页 OCR 置信度较低，请核对原图。")

        def preview(image, number):
            image = image.convert("RGB")
            image.thumbnail((2000, 2400))
            buffer = io.BytesIO()
            image.save(buffer, format="JPEG", quality=88)
            previews.append((number, buffer.getvalue()))
            return image

        if suffix in {".txt", ".md", ".markdown"}:
            encoding = "utf-16" if raw.startswith((b"\xff\xfe", b"\xfe\xff")) else "utf-8-sig"
            try:
                text = raw.decode(encoding)
            except UnicodeDecodeError:
                try:
                    text = raw.decode("gb18030")
                    warnings.append("材料按 GB18030 编码读取，请核对文字。")
                except UnicodeDecodeError:
                    raise ValueError("文字编码无法识别，请保存为 UTF-8 后导入。") from None
            add_page(1, text, "text")
            mime = "text/plain" if suffix == ".txt" else "text/markdown"
        elif suffix == ".docx":
            text = docx_body(raw)
            if not text.strip():
                raise ValueError("DOCX 没有可提取的正文文字。含扫描图的文档可导出 PDF 后导入")
            texts.append(text)
            warnings.append("已按段落读取正文及表格文字。分页、浮动图形和页眉页脚可在原件中核对。")
            mime = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        elif suffix == ".pdf":
            mime = "application/pdf"
            with _pdf_lock:
                try:
                    document = pdfium.PdfDocument(raw)
                except pdfium.PdfiumError:
                    raise ValueError("PDF 无法打开；请检查文件是否损坏或受密码保护。") from None
                try:
                    if not 0 < len(document) <= MAX_PDF_PAGES:
                        raise ValueError("PDF 需包含 1–100 页；请将更长文档分批导入。")
                    for index in range(len(document)):
                        page = document[index]
                        textpage = page.get_textpage()
                        text = textpage.get_text_bounded().replace("\r\n", "\n")
                        textpage.close()
                        scale = min(2, 2400 / max(page.get_size()))
                        bitmap = page.render(scale=scale)
                        image = preview(bitmap.to_pil().copy(), index + 1)
                        bitmap.close()
                        page.close()
                        if len(text.strip()) < 24:
                            ocr_text, confidence = self.ocr(image)
                            if len(ocr_text.strip()) > len(text.strip()):
                                add_page(index + 1, ocr_text, "ocr", confidence)
                            else:
                                add_page(index + 1, text, "pdf_text")
                        else:
                            add_page(index + 1, text, "pdf_text")
                finally:
                    document.close()
            warnings.append("PDF 文字提取可能遗漏图表与布局含义；已保留页面图供视觉模型或人工核对。")
        else:
            try:
                with Image.open(io.BytesIO(raw)) as original:
                    mime = Image.MIME.get(original.format, "image/png")
                    image = preview(ImageOps.exif_transpose(original).copy(), 1)
            except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
                raise ValueError("图片无法读取，请使用有效的 PNG、JPEG 或 WebP 文件。") from None
            text, confidence = self.ocr(image)
            add_page(1, text, "ocr", confidence)
            warnings.append("OCR 只提取文字，照片、图形和空间关系需由视觉模型或人工核对。")
        preview_hashes = {number:hashlib.sha256(data).hexdigest() for number,data in previews}
        pages = [p.model_copy(update={"preview_sha256":preview_hashes.get(p.number)}) for p in pages]
        source = Source(id=f"material_{asset_id[:12]}", name=name, text="\n\n".join(texts),
                        asset_id=asset_id, mime_type=mime, sha256=hashlib.sha256(raw).hexdigest(),
                        pages=pages, warnings=warnings)
        self.directory.mkdir(parents=True,exist_ok=True)
        protect_credentials([name,source.text], credential_configs)
        with TemporaryDirectory(dir=self.directory,prefix="_upload-") as temporary:
            stage = Path(temporary)/"asset"
            stage.mkdir()
            (stage/"original").write_bytes(raw)
            for number, data in previews:
                (stage/f"page-{number}.jpg").write_bytes(data)
            (stage/"source.json").write_text(source.model_dump_json(indent=2),encoding="utf-8")
            stage.rename(folder)
        return source

    def load(self, asset_id: str) -> tuple[Source, Path]:
        if not re.fullmatch(r"[a-f0-9]{32}", asset_id):
            raise ValueError("材料 ID 无效。")
        folder = self.directory / asset_id
        if not (folder / "source.json").is_file():
            raise ValueError("原始材料不在此服务中，请重新导入原件；已有文字草稿仍可使用。")
        return Source.model_validate_json((folder / "source.json").read_text(encoding="utf-8")), folder

    def images(self, sources: list[Source]) -> list[dict]:
        result = []
        for source in sources:
            if not source.asset_id:
                continue
            original, folder = self.load(source.asset_id)
            self.check_metadata(source, original)
            is_visual = original.mime_type == "application/pdf" or (original.mime_type or "").startswith("image/")
            if is_visual and len(result) + len(original.pages) > 20:
                raise ValueError("一次视觉整理最多接收 20 页图片。请分批导入，或关闭图片能力后使用提取文字。")
            for page in original.pages:
                path = folder / f"page-{page.number}.jpg"
                if is_visual and not path.is_file():
                    raise ValueError(f"材料「{source.name}」第 {page.number} 页缺少预览，请重新导入原件")
                if path.exists():
                    raw = path.read_bytes()
                    if page.preview_sha256 and hashlib.sha256(raw).hexdigest() != page.preview_sha256:
                        raise ValueError(f"材料「{source.name}」第 {page.number} 页预览校验失败")
                    result.append({"source_id": source.id, "page": page.number,
                                   "data_url": "data:image/jpeg;base64," + base64.b64encode(raw).decode("ascii")})
        return result

    @staticmethod
    def check_metadata(source, original):
        if (source.sha256, source.text, source.pages, source.mime_type) != (original.sha256, original.text, original.pages, original.mime_type):
            raise ValueError(f"材料「{source.name}」的原件摘要、提取文字或页码与本机原件不符，请另建补充材料")

    def check_sources(self, sources):
        missing = []
        for source in sources:
            if source.asset_id:
                if (self.directory / source.asset_id / "source.json").is_file():
                    original, _ = self.load(source.asset_id)
                    self.check_metadata(source, original)
                else:
                    missing.append(source.name)
        return missing
