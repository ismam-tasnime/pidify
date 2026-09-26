"""Conversion engine: routes (input format, output format) pairs to a backend.

Backends:
  - LibreOffice (headless) for Word / Excel / PowerPoint / OpenDocument files
  - Pandoc for Markdown, HTML, EPUB, reStructuredText, LaTeX
  - PyMuPDF + pdf2docx for PDF input
  - Pillow (+ pillow-heif) for images
"""
from __future__ import annotations

import os
import shutil
import subprocess
import uuid
import zipfile
from pathlib import Path

import logging

import pymupdf as fitz
from PIL import Image, ImageSequence
from pillow_heif import register_heif_opener

register_heif_opener()
logging.getLogger("root").setLevel(logging.WARNING)  # pdf2docx logs every step at INFO


class ConversionError(Exception):
    pass


# --------------------------------------------------------------------------- #
# Tool discovery
# --------------------------------------------------------------------------- #

def _find_tool(env_var: str, name: str, candidates: list[str]) -> str | None:
    if os.environ.get(env_var):
        return os.environ[env_var]
    found = shutil.which(name)
    if found:
        return found
    for c in candidates:
        if Path(c).exists():
            return c
    return None


SOFFICE = _find_tool(
    "SOFFICE_PATH",
    "soffice",
    [
        r"C:\Program Files\LibreOffice\program\soffice.exe",
        r"C:\Program Files (x86)\LibreOffice\program\soffice.exe",
        "/usr/bin/soffice",
        "/Applications/LibreOffice.app/Contents/MacOS/soffice",
    ],
)
PANDOC = _find_tool(
    "PANDOC_PATH",
    "pandoc",
    [
        str(Path(os.environ.get("LOCALAPPDATA", "")) / "Pandoc" / "pandoc.exe"),
        r"C:\Program Files\Pandoc\pandoc.exe",
    ],
)

TIMEOUT = 180  # seconds per external tool call


# --------------------------------------------------------------------------- #
# Format tables
# --------------------------------------------------------------------------- #

WORD_IN = {"doc", "docx", "odt", "rtf", "txt", "wpd", "docm", "dot", "dotx"}
SHEET_IN = {"xls", "xlsx", "ods", "csv", "xlsm", "tsv"}
SLIDE_IN = {"ppt", "pptx", "odp", "pps", "ppsx"}
PANDOC_IN = {"md", "markdown", "html", "htm", "epub", "rst", "tex", "org"}
PDF_IN = {"pdf"}
IMAGE_IN = {"png", "jpg", "jpeg", "webp", "bmp", "gif", "tif", "tiff", "ico", "heic", "heif"}

PAGE_IMAGES = ["png", "jpg"]  # "render every page to an image" outputs

WORD_OUT = ["pdf", "docx", "doc", "odt", "rtf", "txt", "html", "md", "epub", *PAGE_IMAGES]
SHEET_OUT = ["pdf", "xlsx", "xls", "ods", "csv", "html", *PAGE_IMAGES]
SLIDE_OUT = ["pdf", "pptx", "ppt", "odp", *PAGE_IMAGES]
PANDOC_OUT = ["pdf", "docx", "odt", "rtf", "html", "md", "txt", "epub"]
PDF_OUT = ["docx", "txt", "html", *PAGE_IMAGES]
IMAGE_OUT = ["png", "jpg", "webp", "bmp", "gif", "tiff", "ico", "pdf"]

# LibreOffice --convert-to filter strings
LO_FILTERS = {
    "pdf": "pdf",
    "docx": "docx:MS Word 2007 XML",
    "doc": "doc:MS Word 97",
    "odt": "odt",
    "rtf": "rtf",
    "html": "html",
    "xlsx": "xlsx:Calc MS Excel 2007 XML",
    "xls": "xls:MS Excel 97",
    "ods": "ods",
    "csv": "csv:Text - txt - csv (StarCalc):44,34,76,1",
    "pptx": "pptx:Impress MS PowerPoint 2007 XML",
    "ppt": "ppt:MS PowerPoint 97",
    "odp": "odp",
}

PANDOC_READERS = {
    "md": "markdown", "markdown": "markdown", "html": "html", "htm": "html",
    "epub": "epub", "rst": "rst", "tex": "latex", "org": "org",
    "docx": "docx", "odt": "odt",
}
PANDOC_WRITERS = {
    "docx": "docx", "odt": "odt", "rtf": "rtf", "html": "html",
    "md": "gfm", "txt": "plain", "epub": "epub",
}


def ext_of(filename: str) -> str:
    return Path(filename).suffix.lower().lstrip(".")


def targets_for(ext: str) -> list[str]:
    ext = ext.lower()
    if ext in WORD_IN:
        out = WORD_OUT
    elif ext in SHEET_IN:
        out = SHEET_OUT
    elif ext in SLIDE_IN:
        out = SLIDE_OUT
    elif ext in PANDOC_IN:
        out = PANDOC_OUT
    elif ext in PDF_IN:
        out = PDF_OUT
    elif ext in IMAGE_IN:
        out = IMAGE_OUT
    else:
        return []
    norm = {"jpeg": "jpg", "tif": "tiff", "htm": "html", "markdown": "md", "heif": "heic"}
    return [t for t in out if t != norm.get(ext, ext)]


def format_table() -> dict[str, list[str]]:
    all_in = WORD_IN | SHEET_IN | SLIDE_IN | PANDOC_IN | PDF_IN | IMAGE_IN
    return {e: targets_for(e) for e in sorted(all_in)}


def tool_status() -> dict[str, bool]:
    return {"libreoffice": bool(SOFFICE), "pandoc": bool(PANDOC)}


# --------------------------------------------------------------------------- #
# Backends
# --------------------------------------------------------------------------- #

def _run(cmd: list[str], what: str) -> None:
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=TIMEOUT)
    except subprocess.TimeoutExpired:
        raise ConversionError(f"{what} took too long (>{TIMEOUT}s).")
    if proc.returncode != 0:
        msg = (proc.stderr or proc.stdout or "").strip()[-500:]
        raise ConversionError(f"{what} failed: {msg or 'unknown error'}")


def _libreoffice(src: Path, target: str, workdir: Path) -> Path:
    if not SOFFICE:
        raise ConversionError("LibreOffice is not installed on the server.")
    outdir = workdir / f"lo_{uuid.uuid4().hex[:8]}"
    outdir.mkdir()
    # A private profile per call lets several conversions run at once.
    profile = (workdir / f"profile_{uuid.uuid4().hex[:8]}").resolve().as_uri()
    _run(
        [
            SOFFICE, f"-env:UserInstallation={profile}",
            "--headless", "--norestore", "--nolockcheck",
            "--convert-to", LO_FILTERS[target],
            "--outdir", str(outdir), str(src),
        ],
        "LibreOffice",
    )
    produced = [p for p in outdir.iterdir() if p.is_file()]
    if not produced:
        raise ConversionError("LibreOffice did not produce an output file.")
    return produced[0]


def _pandoc(src: Path, src_ext: str, target: str, workdir: Path) -> Path:
    if not PANDOC:
        raise ConversionError("Pandoc is not installed on the server.")
    out = workdir / f"{src.stem}.{target}"
    cmd = [PANDOC, str(src), "-f", PANDOC_READERS[src_ext], "-t", PANDOC_WRITERS[target],
           "-o", str(out), "--resource-path", str(src.parent)]
    if target in {"html", "rtf", "epub"}:
        cmd.append("--standalone")
    if target == "html":
        cmd.append("--embed-resources")
    _run(cmd, "Pandoc")
    return out


def _render_pages(pdf: Path, fmt: str, workdir: Path, stem: str) -> Path:
    """Render each PDF page to an image; one page -> image file, many -> ZIP."""
    doc = fitz.open(pdf)
    try:
        files = []
        for i, page in enumerate(doc, start=1):
            pix = page.get_pixmap(dpi=150, alpha=False)
            p = workdir / f"{stem}_page{i:03d}.{fmt}"
            if fmt == "jpg":
                pix.save(p, jpg_quality=90)
            else:
                pix.save(p)
            files.append(p)
    finally:
        doc.close()
    if len(files) == 1:
        return files[0]
    return _zip(files, workdir / f"{stem}_{fmt}_pages.zip")


def _zip(files: list[Path], dest: Path) -> Path:
    with zipfile.ZipFile(dest, "w", zipfile.ZIP_DEFLATED) as z:
        for f in files:
            z.write(f, f.name)
    return dest


def _pdf(src: Path, target: str, workdir: Path, stem: str) -> Path:
    if target in PAGE_IMAGES:
        return _render_pages(src, target, workdir, stem)
    out = workdir / f"{stem}.{target}"
    if target == "docx":
        from pdf2docx import Converter
        cv = Converter(str(src))
        try:
            cv.convert(str(out))
        finally:
            cv.close()
        return out
    doc = fitz.open(src)
    try:
        if target == "txt":
            out.write_text("\n\f\n".join(p.get_text() for p in doc), encoding="utf-8")
        elif target == "html":
            body = "\n".join(p.get_text("html") for p in doc)
            out.write_text(f"<!doctype html><meta charset='utf-8'><title>{stem}</title>\n{body}",
                           encoding="utf-8")
    finally:
        doc.close()
    return out


PIL_FORMATS = {"png": "PNG", "jpg": "JPEG", "webp": "WEBP", "bmp": "BMP",
               "gif": "GIF", "tiff": "TIFF", "ico": "ICO", "pdf": "PDF"}


def _image(src: Path, target: str, workdir: Path, stem: str) -> Path:
    out = workdir / f"{stem}.{target}"
    try:
        img = Image.open(src)
    except Exception as e:
        raise ConversionError(f"Could not read image: {e}")

    # Keep animation for GIF/WEBP -> GIF/WEBP; multi-page TIFF -> multi-page PDF.
    frames = [f.copy() for f in ImageSequence.Iterator(img)]
    multi = len(frames) > 1 and target in {"gif", "webp", "pdf", "tiff"}

    def prep(im: Image.Image) -> Image.Image:
        if target in {"jpg", "bmp", "pdf"}:
            if im.mode in ("RGBA", "LA", "P"):
                im = im.convert("RGBA")
                bg = Image.new("RGB", im.size, "white")
                bg.paste(im, mask=im.split()[-1])
                return bg
            return im.convert("RGB")
        if target == "ico":
            im = im.convert("RGBA")
            im.thumbnail((256, 256))
        return im

    frames = [prep(f) for f in (frames if multi else frames[:1])]
    kwargs = {}
    if target == "jpg":
        kwargs["quality"] = 92
    if target == "webp":
        kwargs["quality"] = 90
    if multi:
        kwargs.update(save_all=True, append_images=frames[1:])
        if target in {"gif", "webp"}:
            kwargs.update(loop=img.info.get("loop", 0), duration=img.info.get("duration", 100))
    frames[0].save(out, PIL_FORMATS[target], **kwargs)
    return out


# --------------------------------------------------------------------------- #
# Router
# --------------------------------------------------------------------------- #

def convert(src: Path, target: str, workdir: Path) -> Path:
    """Convert `src` into `target` format inside `workdir`; return the output path."""
    ext = ext_of(src.name)
    target = target.lower()
    if target not in targets_for(ext):
        raise ConversionError(f"Converting .{ext} to .{target} isn't supported.")
    stem = src.stem

    if ext in IMAGE_IN:
        return _image(src, target, workdir, stem)

    if ext in PDF_IN:
        return _pdf(src, target, workdir, stem)

    if ext in PANDOC_IN:
        if target == "pdf":  # Pandoc -> DOCX -> LibreOffice -> PDF
            return _libreoffice(_pandoc(src, ext, "docx", workdir), "pdf", workdir)
        return _pandoc(src, ext, target, workdir)

    # Office documents (Word / Excel / PowerPoint / OpenDocument)
    if target in PAGE_IMAGES:
        pdf = _libreoffice(src, "pdf", workdir)
        return _render_pages(pdf, target, workdir, stem)
    if target in {"md", "epub", "txt"}:  # Word-type only: go through DOCX + Pandoc
        docx = src if ext == "docx" else _libreoffice(src, "docx", workdir)
        return _pandoc(docx, "docx", target, workdir)
    return _libreoffice(src, target, workdir)


# --------------------------------------------------------------------------- #
# Merge
# --------------------------------------------------------------------------- #

def can_merge(ext: str) -> bool:
    return ext in PDF_IN or "pdf" in targets_for(ext)


def merge_pdfs(sources: list[Path], workdir: Path, out_name: str = "merged") -> Path:
    """Combine files into one PDF in the given order. Non-PDF inputs are converted first."""
    if len(sources) < 2:
        raise ConversionError("Add at least two files to merge.")
    merged = fitz.open()
    try:
        for src in sources:
            ext = ext_of(src.name)
            if not can_merge(ext):
                raise ConversionError(f"{src.name}: .{ext} files can't be turned into PDF.")
            if ext in PDF_IN:
                pdf = src
            else:
                sub = workdir / f"part_{uuid.uuid4().hex[:8]}"
                sub.mkdir()
                pdf = convert(src, "pdf", sub)
            try:
                part = fitz.open(pdf)
            except Exception:
                raise ConversionError(f"{src.name} isn't a valid PDF.")
            try:
                if part.needs_pass:
                    raise ConversionError(f"{src.name} is password-protected. Unlock it first.")
                merged.insert_pdf(part)
            finally:
                part.close()
        out = workdir / f"{out_name}.pdf"
        merged.save(out, garbage=3, deflate=True)
    finally:
        merged.close()
    return out
