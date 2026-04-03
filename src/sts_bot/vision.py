from __future__ import annotations

import ctypes
import re
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from shutil import which
from typing import Iterable

import numpy as np
from PIL import Image, ImageGrab, ImageOps

from sts_bot.config import Rect, TextRegionDefinition

user32 = ctypes.windll.user32
SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77


@dataclass(slots=True)
class TemplateMatch:
    score: float
    point: tuple[int, int]
    found: bool


def capture_rect(rect: Rect) -> Image.Image:
    # PIL bbox capture can fail on secondary monitors; crop from the virtual desktop instead.
    virtual_left = user32.GetSystemMetrics(SM_XVIRTUALSCREEN)
    virtual_top = user32.GetSystemMetrics(SM_YVIRTUALSCREEN)
    desktop = ImageGrab.grab(all_screens=True)
    crop_box = (
        rect.left - virtual_left,
        rect.top - virtual_top,
        rect.left - virtual_left + rect.width,
        rect.top - virtual_top + rect.height,
    )
    return desktop.crop(crop_box)


def grayscale_array(image: Image.Image) -> np.ndarray:
    gray = ImageOps.grayscale(image)
    return np.asarray(gray, dtype=np.float32)


def load_template(path: Path, *, scale_x: float = 1.0, scale_y: float = 1.0) -> np.ndarray:
    return _load_template_cached(str(path), round(scale_x, 4), round(scale_y, 4))


@lru_cache(maxsize=512)
def _load_template_cached(path: str, scale_x: float, scale_y: float) -> np.ndarray:
    image = Image.open(path)
    if scale_x != 1.0 or scale_y != 1.0:
        scaled_size = (
            max(1, round(image.width * scale_x)),
            max(1, round(image.height * scale_y)),
        )
        image = image.resize(scaled_size)
    return grayscale_array(image)


def crop(image: Image.Image, rect: Rect) -> Image.Image:
    box = (rect.left, rect.top, rect.left + rect.width, rect.top + rect.height)
    return image.crop(box)


def match_template(
    image: Image.Image,
    template_path: Path,
    region: Rect,
    threshold: float,
    *,
    scale_x: float = 1.0,
    scale_y: float = 1.0,
) -> TemplateMatch:
    search_image = crop(image, region)
    search = grayscale_array(search_image)
    template = load_template(template_path, scale_x=scale_x, scale_y=scale_y)

    search_h, search_w = search.shape
    template_h, template_w = template.shape
    if search_h < template_h or search_w < template_w:
        return TemplateMatch(score=0.0, point=(region.left, region.top), found=False)

    cv2_match = _opencv_match(search, template)
    if cv2_match is not None:
        score, left, top = cv2_match
        best_point = (region.left + left, region.top + top)
        return TemplateMatch(score=score, point=best_point, found=score >= threshold)

    best_score = -1.0
    best_point = (region.left, region.top)
    for top in range(search_h - template_h + 1):
        bottom = top + template_h
        for left in range(search_w - template_w + 1):
            right = left + template_w
            patch = search[top:bottom, left:right]
            score = _normalized_correlation(patch, template)
            if score > best_score:
                best_score = score
                best_point = (region.left + left, region.top + top)

    return TemplateMatch(score=best_score, point=best_point, found=best_score >= threshold)


def extract_text(image: Image.Image, definition: TextRegionDefinition) -> str:
    return _extract_text_with_settings(
        image,
        definition,
        psm_values=(7, 8, 10, 13),
        variant_limit=5,
    )


def extract_text_fast(image: Image.Image, definition: TextRegionDefinition) -> str:
    return _extract_text_with_settings(
        image,
        definition,
        psm_values=(7, 8),
        variant_limit=2,
    )


def _extract_text_with_settings(
    image: Image.Image,
    definition: TextRegionDefinition,
    *,
    psm_values: tuple[int, ...],
    variant_limit: int,
) -> str:
    roi = crop(image, definition.region)
    try:
        import pytesseract
    except ImportError as exc:
        raise RuntimeError("pytesseract is required for OCR-based probing.") from exc

    tesseract_cmd = _detect_tesseract_cmd()
    if tesseract_cmd is None:
        raise RuntimeError("Tesseract executable was not found.")
    pytesseract.pytesseract.tesseract_cmd = str(tesseract_cmd)

    whitelist_config = ""
    if definition.whitelist:
        whitelist_config = f" -c tessedit_char_whitelist={definition.whitelist}"

    candidates: list[str] = []
    for processed in _prepare_ocr_variants(roi)[: max(1, variant_limit)]:
        for psm in psm_values:
            text = pytesseract.image_to_string(processed, config=f"--psm {psm}{whitelist_config}").strip()
            if text:
                candidates.append(text)

    return _choose_best_ocr_candidate(candidates, definition.whitelist)


def parse_text_value(text: str, parser: str) -> int | tuple[int, int] | str | None:
    if not text:
        return None
    if parser == "int":
        match = re.search(r"\d+", text)
        return int(match.group()) if match else None
    if parser == "pair":
        match = re.search(r"(\d+)\s*/\s*(\d+)", text)
        return (int(match.group(1)), int(match.group(2))) if match else None
    return text


def _prepare_ocr_variants(image: Image.Image) -> list[Image.Image]:
    gray = ImageOps.grayscale(image)
    enlarged = gray.resize((gray.width * 4, gray.height * 4))
    auto = ImageOps.autocontrast(enlarged)
    return [
        auto,
        auto.point(lambda pixel: 255 if pixel > 115 else 0),
        auto.point(lambda pixel: 255 if pixel > 135 else 0),
        ImageOps.invert(auto),
        ImageOps.invert(auto).point(lambda pixel: 255 if pixel > 150 else 0),
    ]


@lru_cache(maxsize=1)
def _detect_tesseract_cmd() -> Path | None:
    direct = which("tesseract")
    if direct:
        return Path(direct)

    candidates = [
        Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe"),
        Path(r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe"),
    ]
    for candidate in candidates:
        if candidate.exists():
            return candidate
    return None


def _normalized_correlation(patch: np.ndarray, template: np.ndarray) -> float:
    patch_centered = patch - patch.mean()
    template_centered = template - template.mean()
    denominator = float(np.sqrt((patch_centered * patch_centered).sum() * (template_centered * template_centered).sum()))
    if denominator == 0.0:
        return 0.0
    return float((patch_centered * template_centered).sum() / denominator)


def _opencv_match(search: np.ndarray, template: np.ndarray) -> tuple[float, int, int] | None:
    try:
        import cv2
    except ImportError:
        return None
    search_image = search.astype(np.uint8, copy=False)
    template_image = template.astype(np.uint8, copy=False)
    result = cv2.matchTemplate(search_image, template_image, cv2.TM_CCOEFF_NORMED)
    _, max_score, _, max_loc = cv2.minMaxLoc(result)
    return (float(max_score), int(max_loc[0]), int(max_loc[1]))


def _choose_best_ocr_candidate(candidates: Iterable[str], whitelist: str | None) -> str:
    best = ""
    best_score = -1
    allowed = set(whitelist) if whitelist else None
    for candidate in candidates:
        normalized = candidate.strip()
        if not normalized:
            continue
        score = len(normalized)
        if allowed is not None:
            score += sum(2 for char in normalized if char in allowed)
            score -= sum(3 for char in normalized if char not in allowed)
        if score > best_score:
            best = normalized
            best_score = score
    return best
