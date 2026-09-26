"""Pidify web server — upload a file, pick a format, download the result."""
from __future__ import annotations

import json
import re
import shutil
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask

from . import converters, scanner

MAX_UPLOAD_MB = 100
MAX_MERGE_FILES = 50
MAX_MERGE_MB = 300
STATIC_DIR = Path(__file__).resolve().parent.parent / "static"

app = FastAPI(title="Pidify", version="1.0.0")


def _safe_name(name: str) -> str:
    name = Path(name or "file").name
    name = re.sub(r"[^\w.\- ()]+", "_", name).strip(" .")
    return name or "file"


@app.get("/api/formats")
def formats():
    return {"formats": converters.format_table(), "tools": converters.tool_status(),
            "max_upload_mb": MAX_UPLOAD_MB, "max_merge_files": MAX_MERGE_FILES,
            "max_merge_mb": MAX_MERGE_MB}


@app.post("/api/convert")
async def convert(file: UploadFile = File(...), target: str = Form(...)):
    filename = _safe_name(file.filename)
    ext = converters.ext_of(filename)
    if not converters.targets_for(ext):
        raise HTTPException(400, f"Files of type .{ext or '?'} aren't supported.")

    workdir = Path(tempfile.mkdtemp(prefix="pidify_"))
    cleanup = BackgroundTask(shutil.rmtree, workdir, ignore_errors=True)
    try:
        src = workdir / filename
        size = 0
        with src.open("wb") as f:
            while chunk := await file.read(1024 * 1024):
                size += len(chunk)
                if size > MAX_UPLOAD_MB * 1024 * 1024:
                    raise HTTPException(413, f"File is larger than {MAX_UPLOAD_MB} MB.")
                f.write(chunk)
        if size == 0:
            raise HTTPException(400, "The uploaded file is empty.")

        out = await run_in_threadpool(converters.convert, src, target, workdir)
    except converters.ConversionError as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(422, str(e))
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(500, f"Unexpected error: {e}")

    download_name = out.name if out.suffix == ".zip" else f"{Path(filename).stem}.{target.lower()}"
    return FileResponse(out, filename=download_name, background=cleanup)


@app.post("/api/merge")
async def merge(files: list[UploadFile] = File(...), name: str = Form("merged")):
    """Merge the uploaded files, in upload order, into a single PDF."""
    if len(files) > MAX_MERGE_FILES:
        raise HTTPException(400, f"You can merge up to {MAX_MERGE_FILES} files at once.")
    out_name = Path(_safe_name(name)).stem or "merged"

    workdir = Path(tempfile.mkdtemp(prefix="pidify_merge_"))
    cleanup = BackgroundTask(shutil.rmtree, workdir, ignore_errors=True)
    try:
        sources, total = [], 0
        for i, upload in enumerate(files):
            filename = _safe_name(upload.filename)
            if not converters.can_merge(converters.ext_of(filename)):
                raise HTTPException(400, f"{filename}: this file type can't be merged into a PDF.")
            # One folder per file keeps the original name even if two files share it.
            (workdir / f"{i:03d}").mkdir()
            src = workdir / f"{i:03d}" / filename
            with src.open("wb") as f:
                while chunk := await upload.read(1024 * 1024):
                    total += len(chunk)
                    if total > MAX_MERGE_MB * 1024 * 1024:
                        raise HTTPException(413, f"Files add up to more than {MAX_MERGE_MB} MB.")
                    f.write(chunk)
            sources.append(src)

        out = await run_in_threadpool(converters.merge_pdfs, sources, workdir, out_name)
    except converters.ConversionError as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(422, str(e))
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(500, f"Unexpected error: {e}")

    return FileResponse(out, filename=out.name, background=cleanup)


# --------------------------------------------------------------------------- #
# Scan to PDF (phone photos of paper)
# --------------------------------------------------------------------------- #

def _scan_options(mode: str, crop: bool, marks: bool, light: bool) -> scanner.ScanOptions:
    if mode not in {"color", "gray", "bw"}:
        raise HTTPException(400, "Mode must be color, gray or bw.")
    return scanner.ScanOptions(crop=crop, remove_marks=marks, fix_light=light, mode=mode)


async def _save_upload(upload: UploadFile, dest: Path, used: int, limit_mb: int) -> int:
    with dest.open("wb") as f:
        while chunk := await upload.read(1024 * 1024):
            used += len(chunk)
            if used > limit_mb * 1024 * 1024:
                raise HTTPException(413, f"Photos add up to more than {limit_mb} MB.")
            f.write(chunk)
    return used


def _check_photo(filename: str) -> None:
    if converters.ext_of(filename) not in converters.IMAGE_IN:
        raise HTTPException(400, f"{filename}: only photos (JPG, PNG, HEIC, WEBP…) can be scanned.")


@app.post("/api/scan/preview")
async def scan_preview(file: UploadFile = File(...), mode: str = Form("color"),
                       crop: bool = Form(True), marks: bool = Form(True), light: bool = Form(True)):
    """Clean one photo and return a JPEG preview. X-Scan-Info says what was done."""
    opts = _scan_options(mode, crop, marks, light)
    filename = _safe_name(file.filename)
    _check_photo(filename)
    workdir = Path(tempfile.mkdtemp(prefix="pidify_scan_"))
    cleanup = BackgroundTask(shutil.rmtree, workdir, ignore_errors=True)
    try:
        src = workdir / filename
        await _save_upload(file, src, 0, MAX_UPLOAD_MB)

        def work():
            arr, info = scanner.clean(scanner.load_photo(src), opts)
            return scanner.preview_jpeg(arr, workdir / "preview.jpg"), info

        out, info = await run_in_threadpool(work)
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(422, f"{filename}: couldn't read this photo ({e}).")

    header = json.dumps({"page_found": info.page_found, "marks_removed": info.marks_removed,
                         "notes": info.notes})
    return FileResponse(out, media_type="image/jpeg", background=cleanup,
                        headers={"X-Scan-Info": header, "Cache-Control": "no-store"})


@app.post("/api/scan")
async def scan(files: list[UploadFile] = File(...), mode: str = Form("color"),
               crop: bool = Form(True), marks: bool = Form(True), light: bool = Form(True),
               name: str = Form("scan")):
    """Clean every photo and put them, in order, into one PDF (one photo per page)."""
    opts = _scan_options(mode, crop, marks, light)
    if len(files) > MAX_MERGE_FILES:
        raise HTTPException(400, f"You can scan up to {MAX_MERGE_FILES} photos at once.")
    out_name = Path(_safe_name(name)).stem or "scan"

    workdir = Path(tempfile.mkdtemp(prefix="pidify_scan_"))
    cleanup = BackgroundTask(shutil.rmtree, workdir, ignore_errors=True)
    try:
        sources, used = [], 0
        for i, upload in enumerate(files):
            filename = _safe_name(upload.filename)
            _check_photo(filename)
            (workdir / f"{i:03d}").mkdir()
            src = workdir / f"{i:03d}" / filename
            used = await _save_upload(upload, src, used, MAX_MERGE_MB)
            sources.append(src)

        def work():
            pages = []
            for src in sources:
                try:
                    arr, _ = scanner.clean(scanner.load_photo(src), opts)
                except Exception as e:
                    raise converters.ConversionError(f"{src.name}: couldn't read this photo ({e}).")
                pages.append(scanner.to_pil(arr, opts.mode))
            return scanner.save_pdf(pages, workdir / f"{out_name}.pdf")

        out = await run_in_threadpool(work)
    except converters.ConversionError as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(422, str(e))
    except HTTPException:
        shutil.rmtree(workdir, ignore_errors=True)
        raise
    except Exception as e:
        shutil.rmtree(workdir, ignore_errors=True)
        raise HTTPException(500, f"Unexpected error: {e}")

    return FileResponse(out, filename=out.name, background=cleanup)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
