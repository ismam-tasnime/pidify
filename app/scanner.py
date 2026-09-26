"""Turn phone photos of paper into clean, flat scans.

Pipeline for each photo:
  1. Fix orientation from EXIF, shrink huge photos.
  2. Find the page (the largest bright four-sided shape) and flatten it.
  3. Find fingers/thumbs on the page (skin-coloured blobs entering from the edge).
  4. Even out the lighting so shadows and yellow tint disappear.
  5. Paint the finger areas as clean paper.
  6. Colour, grayscale, or black & white output.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

# 2400 px on the long side is ~290 DPI on an A4 page: sharper than a typical office
# scanner, while keeping memory low enough for small (512 MB) servers.
MAX_SIDE = 2400
PREVIEW_SIDE = 1400
DETECT_SIDE = 900


@dataclass
class ScanOptions:
    crop: bool = True
    remove_marks: bool = True
    fix_light: bool = True
    mode: str = "color"  # color | gray | bw


@dataclass
class ScanInfo:
    page_found: bool = False
    marks_removed: int = 0
    notes: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------- #
# Loading
# --------------------------------------------------------------------------- #

def load_photo(path: Path, max_side: int = MAX_SIDE) -> np.ndarray:
    """Load any supported image as BGR, honouring EXIF rotation, at most `max_side` px."""
    img = Image.open(path)
    # For JPEGs, decode straight at a reduced size: a 12 MP photo never
    # has to exist in memory at full resolution.
    img.draft("RGB", (max_side, max_side))
    img = ImageOps.exif_transpose(img)
    img.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("RGB")
    return cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)


# --------------------------------------------------------------------------- #
# Page detection + flattening
# --------------------------------------------------------------------------- #

def _order(pts: np.ndarray) -> np.ndarray:
    pts = pts.reshape(4, 2).astype(np.float32)
    s, d = pts.sum(1), np.diff(pts, axis=1).ravel()
    return np.array([pts[s.argmin()], pts[d.argmin()], pts[s.argmax()], pts[d.argmax()]], np.float32)


def _quad_from_contour(cnt: np.ndarray) -> np.ndarray | None:
    # The convex hull ignores notches made by fingers overlapping the page edge.
    hull = cv2.convexHull(cnt)
    peri = cv2.arcLength(hull, True)
    for eps in (0.02, 0.03, 0.04, 0.06, 0.08):
        approx = cv2.approxPolyDP(hull, eps * peri, True)
        if len(approx) == 4 and cv2.isContourConvex(approx):
            return approx.reshape(4, 2)
    return None


def find_page(img: np.ndarray) -> np.ndarray | None:
    """Return the page corners (in `img` coordinates) or None."""
    h, w = img.shape[:2]
    s = DETECT_SIDE / max(h, w)
    small = cv2.resize(img, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    area_total = small.shape[0] * small.shape[1]
    gray = cv2.GaussianBlur(cv2.cvtColor(small, cv2.COLOR_BGR2GRAY), (5, 5), 0)

    candidates = []
    # Strategy A: paper is brighter than what's around it.
    _, bright = cv2.threshold(gray, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bright = cv2.morphologyEx(bright, cv2.MORPH_CLOSE, np.ones((9, 9), np.uint8))
    # Strategy B: strong edges around the page.
    edges = cv2.dilate(cv2.Canny(gray, 40, 120), np.ones((3, 3), np.uint8), iterations=2)

    for mask in (bright, edges):
        cnts, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        for c in sorted(cnts, key=cv2.contourArea, reverse=True)[:5]:
            q = _quad_from_contour(c)
            if q is None:
                continue
            a = cv2.contourArea(q.astype(np.float32))
            # Ignore tiny shapes, and "the whole photo" (nothing to crop).
            if 0.15 * area_total < a < 0.985 * area_total:
                candidates.append((a, q))
    if not candidates:
        return None
    _, best = max(candidates, key=lambda t: t[0])
    return _order(best / s)


def flatten(img: np.ndarray, quad: np.ndarray) -> np.ndarray:
    tl, tr, br, bl = quad
    width = int(max(np.linalg.norm(tr - tl), np.linalg.norm(br - bl)))
    height = int(max(np.linalg.norm(bl - tl), np.linalg.norm(br - tr)))
    dst = np.array([[0, 0], [width - 1, 0], [width - 1, height - 1], [0, height - 1]], np.float32)
    m = cv2.getPerspectiveTransform(quad, dst)
    out = cv2.warpPerspective(img, m, (width, height), flags=cv2.INTER_CUBIC,
                              borderMode=cv2.BORDER_REPLICATE)
    # Trim a hair off each side to lose the table peeking in at the edges.
    t = max(2, round(min(width, height) * 0.006))
    return out[t:-t, t:-t]


# --------------------------------------------------------------------------- #
# Finger / mark detection
# --------------------------------------------------------------------------- #

def find_fingers(img: np.ndarray) -> tuple[np.ndarray, int]:
    """Mask of skin-coloured blobs that reach in from the page edge."""
    h, w = img.shape[:2]
    ycc = cv2.cvtColor(img, cv2.COLOR_BGR2YCrCb)
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)
    _, cr, cb = cv2.split(ycc)
    hue, sat, val = cv2.split(hsv)
    skin = (
        (cr >= 138) & (cr <= 180) & (cb >= 77) & (cb <= 130)
        & (sat >= 45) & (val >= 50)
        & ((hue <= 25) | (hue >= 165))  # reds/oranges/browns in OpenCV's 0-179 scale
    ).astype(np.uint8) * 255

    k = max(3, round(min(h, w) / 150)) | 1
    skin = cv2.morphologyEx(skin, cv2.MORPH_OPEN, np.ones((k, k), np.uint8))
    skin = cv2.morphologyEx(skin, cv2.MORPH_CLOSE, np.ones((k * 3, k * 3), np.uint8))

    n, labels, stats, _ = cv2.connectedComponentsWithStats(skin, connectivity=8)
    mask = np.zeros((h, w), np.uint8)
    margin = max(4, round(min(h, w) * 0.03))
    total = h * w
    kept = 0
    for i in range(1, n):
        x, y, bw, bh, area = stats[i]
        touches_edge = x <= margin or y <= margin or x + bw >= w - margin or y + bh >= h - margin
        # Fingers come in from the side; big enough to matter, not so big it's a coloured page.
        if touches_edge and 0.002 * total <= area <= 0.25 * total:
            mask[labels == i] = 255
            kept += 1
    if kept:
        # Grow the mask to cover the finger's soft shadow and blurry outline.
        grow = max(5, round(min(h, w) / 60))
        mask = cv2.dilate(mask, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (grow, grow)))
    return mask, kept


# --------------------------------------------------------------------------- #
# Lighting + output
# --------------------------------------------------------------------------- #

def even_lighting(img: np.ndarray, ignore: np.ndarray | None = None) -> np.ndarray:
    """Divide out the paper's brightness so shadows and tint vanish, keeping ink colours."""
    h, w = img.shape[:2]
    # The paper's brightness is a smooth, blurry map, so estimate it on a small
    # copy (about 16x fewer pixels) and scale it back up. Much faster, same result.
    s = min(1.0, 600 / max(h, w))
    sw, sh = max(1, round(w * s)), max(1, round(h * s))
    small = cv2.resize(img, (sw, sh), interpolation=cv2.INTER_AREA)
    k = max(5, round(min(sh, sw) / 25)) | 1
    if ignore is not None and ignore.any():
        # Keep fingers from darkening the estimated paper around them.
        small_mask = cv2.resize(ignore, (sw, sh), interpolation=cv2.INTER_NEAREST)
        small = cv2.inpaint(small, small_mask, 3, cv2.INPAINT_TELEA)
    # Closing wipes out text (dark, thin), leaving the paper; blur smooths it.
    bg = cv2.morphologyEx(small, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(bg, (0, 0), k / 2)
    bg = cv2.max(cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR), 1)
    # uint8 divide saturates at 255 by itself: no float copies of the full image.
    out = cv2.divide(img, bg, scale=255)
    # Gentle contrast curve: near-white -> white, ink stays strong.
    lut = np.clip((np.arange(256) - 25) * 255 / 215, 0, 255).astype(np.uint8)
    return cv2.LUT(out, lut)


def paint_paper(img: np.ndarray, mask: np.ndarray, paper: tuple[int, int, int]) -> np.ndarray:
    """Fill `mask` with plain paper colour, with a soft edge."""
    alpha = cv2.GaussianBlur(mask, (0, 0), 3).astype(np.float32)[..., None] / 255
    fill = np.empty_like(img)
    fill[:] = paper
    return (img * (1 - alpha) + fill * alpha).astype(np.uint8)


def to_mode(img: np.ndarray, mode: str, evened: bool = False) -> np.ndarray:
    if mode == "gray":
        return cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
    if mode == "bw":
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        if evened:
            # Paper is already pure white, so one cut-off is cleanest.
            _, bw = cv2.threshold(gray, 200, 255, cv2.THRESH_BINARY)
        else:
            bw = cv2.adaptiveThreshold(gray, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C,
                                       cv2.THRESH_BINARY, 31, 10)
        # Drop single-pixel dust (small enough that dots on i and j survive).
        n, labels, stats, _ = cv2.connectedComponentsWithStats(255 - bw, connectivity=8)
        tiny = np.isin(labels, np.where(stats[:, cv2.CC_STAT_AREA] <= 2)[0][1:])
        bw[tiny] = 255
        return bw
    return img


# Clean one photo at a time across the whole server (previews and PDFs alike),
# so memory stays flat on small machines no matter how many requests arrive.
_one_at_a_time = threading.Lock()


def clean(img: np.ndarray, opts: ScanOptions) -> tuple[np.ndarray, ScanInfo]:
    with _one_at_a_time:
        return _clean(img, opts)


def _clean(img: np.ndarray, opts: ScanOptions) -> tuple[np.ndarray, ScanInfo]:
    info = ScanInfo()
    if opts.crop:
        quad = find_page(img)
        if quad is not None:
            img = flatten(img, quad)
            info.page_found = True
        else:
            info.notes.append("Couldn't find the page edges, so the photo was kept uncropped.")

    mask = None
    if opts.remove_marks:
        mask, info.marks_removed = find_fingers(img)

    paper = (255, 255, 255)
    if opts.fix_light:
        img = even_lighting(img, mask)
    elif mask is not None and mask.any():
        # Without lighting correction, match the page's own paper colour.
        rest = img[mask == 0]
        paper = tuple(int(v) for v in np.percentile(rest, 90, axis=0)) if rest.size else paper

    if mask is not None and mask.any():
        img = paint_paper(img, mask, paper)
    return to_mode(img, opts.mode, evened=opts.fix_light), info


# --------------------------------------------------------------------------- #
# Output helpers
# --------------------------------------------------------------------------- #

A4_WIDTH_PT = 595  # PDF points (1/72 inch)


def encode_page(arr: np.ndarray, mode: str) -> bytes:
    """Compress one cleaned page: PNG for black & white (tiny and crisp), JPEG otherwise."""
    if mode == "bw":
        ok, buf = cv2.imencode(".png", arr, [cv2.IMWRITE_PNG_BILEVEL, 1])
    else:
        ok, buf = cv2.imencode(".jpg", arr, [cv2.IMWRITE_JPEG_QUALITY, 88])
    if not ok:
        raise ValueError("couldn't encode page")
    return buf.tobytes()


def build_pdf(photos: list[Path], opts: ScanOptions, dest: Path,
              on_error=None) -> Path:
    """Clean each photo and add it to the PDF straight away, one page at a time.

    Only one photo is ever held uncompressed, so memory stays flat whether
    there are 3 photos or 50.
    """
    import pymupdf

    doc = pymupdf.open()
    try:
        for photo in photos:
            try:
                arr, _ = clean(load_photo(photo), opts)
            except Exception as e:
                if on_error:
                    raise on_error(photo, e)
                raise
            h, w = arr.shape[:2]
            data = encode_page(arr, opts.mode)
            del arr
            # Every page is A4 width; height follows the photo's shape.
            page = doc.new_page(width=A4_WIDTH_PT, height=A4_WIDTH_PT * h / w)
            page.insert_image(page.rect, stream=data)
        doc.save(dest, garbage=3, deflate=True)
    finally:
        doc.close()
    return dest


def preview_jpeg(arr: np.ndarray, dest: Path, max_side: int = 900) -> Path:
    h, w = arr.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1:
        arr = cv2.resize(arr, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(dest), arr, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return dest
