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

from dataclasses import dataclass, field
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageOps

MAX_SIDE = 3000
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

def load_photo(path: Path) -> np.ndarray:
    """Load any supported image as BGR, honouring EXIF rotation."""
    img = Image.open(path)
    img = ImageOps.exif_transpose(img)
    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img, mask=img.split()[-1])
        img = bg
    img = img.convert("RGB")
    arr = cv2.cvtColor(np.asarray(img), cv2.COLOR_RGB2BGR)
    h, w = arr.shape[:2]
    if max(h, w) > MAX_SIDE:
        s = MAX_SIDE / max(h, w)
        arr = cv2.resize(arr, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    return arr


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
    k = max(15, round(min(h, w) / 25)) | 1
    work = img.copy()
    if ignore is not None and ignore.any():
        # Keep fingers from darkening the estimated paper around them.
        work = cv2.inpaint(work, ignore, 5, cv2.INPAINT_TELEA)
    # Closing wipes out text (dark, thin), leaving the paper; blur smooths it.
    bg = cv2.morphologyEx(work, cv2.MORPH_CLOSE, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (k, k)))
    bg = cv2.GaussianBlur(bg, (0, 0), k / 2)
    out = cv2.divide(img.astype(np.float32), np.maximum(bg.astype(np.float32), 1), scale=255)
    out = np.clip(out, 0, 255).astype(np.uint8)
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


def clean(img: np.ndarray, opts: ScanOptions) -> tuple[np.ndarray, ScanInfo]:
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

A4_WIDTH_IN = 8.27


def to_pil(arr: np.ndarray, mode: str) -> Image.Image:
    if arr.ndim == 2:
        im = Image.fromarray(arr)
        return im.convert("1", dither=Image.Dither.NONE) if mode == "bw" else im
    return Image.fromarray(cv2.cvtColor(arr, cv2.COLOR_BGR2RGB))


def save_pdf(pages: list[Image.Image], dest: Path) -> Path:
    # Pick a DPI so each page comes out roughly A4-wide.
    first, rest = pages[0], pages[1:]
    dpi = max(72, round(first.width / A4_WIDTH_IN))
    first.save(dest, "PDF", resolution=dpi, save_all=True, append_images=rest, quality=88)
    return dest


def preview_jpeg(arr: np.ndarray, dest: Path, max_side: int = 900) -> Path:
    h, w = arr.shape[:2]
    s = min(1.0, max_side / max(h, w))
    if s < 1:
        arr = cv2.resize(arr, (round(w * s), round(h * s)), interpolation=cv2.INTER_AREA)
    cv2.imwrite(str(dest), arr, [cv2.IMWRITE_JPEG_QUALITY, 82])
    return dest
