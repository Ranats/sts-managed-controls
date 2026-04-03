from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw
from sts_bot.config import CalibrationProfile

from sts_bot.config import Rect


def parse_rect(raw: str) -> Rect:
    parts = [int(part.strip()) for part in raw.split(",")]
    return Rect.from_list(parts)


def crop_to_file(image_path: Path, rect: Rect, output_path: Path) -> Path:
    image = Image.open(image_path)
    box = (rect.left, rect.top, rect.left + rect.width, rect.top + rect.height)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.crop(box).save(output_path)
    return output_path


def annotate_profile(image_path: Path, profile: CalibrationProfile, output_path: Path) -> Path:
    image = Image.open(image_path).convert("RGB")
    scale_x = image.width / profile.reference_width
    scale_y = image.height / profile.reference_height
    draw = ImageDraw.Draw(image)

    for anchor in profile.anchors:
        rect = anchor.region.scaled(scale_x, scale_y)
        draw.rectangle(_rect_points(rect), outline=(255, 0, 0), width=2)
        draw.text((rect.left, max(0, rect.top - 18)), f"A:{anchor.name}", fill=(255, 0, 0))

    for region in profile.text_regions:
        rect = region.region.scaled(scale_x, scale_y)
        draw.rectangle(_rect_points(rect), outline=(0, 255, 0), width=2)
        draw.text((rect.left, max(0, rect.top - 18)), f"T:{region.name}", fill=(0, 255, 0))

    for action in profile.actions:
        point_x = round(action.point[0] * scale_x)
        point_y = round(action.point[1] * scale_y)
        radius = 9
        draw.ellipse((point_x - radius, point_y - radius, point_x + radius, point_y + radius), outline=(0, 128, 255), width=2)
        draw.text((point_x + 12, max(0, point_y - 12)), f"P:{action.label}", fill=(0, 128, 255))

    output_path.parent.mkdir(parents=True, exist_ok=True)
    image.save(output_path)
    return output_path


def _rect_points(rect: Rect) -> tuple[int, int, int, int]:
    return (rect.left, rect.top, rect.left + rect.width, rect.top + rect.height)
