"""Спільне для PNG-карток (PnL і статистика): палітра, шрифт, полотно, збереження."""

import io
import os
from typing import Optional

from PIL import Image, ImageDraw, ImageFont, ImageOps


FONT_PATH = os.path.join(os.path.dirname(__file__), "Inter-Regular.ttf")

CARD_W = 720
CARD_H = 780

BG_COLOR = (14, 15, 18)
CARD_RADIUS = 28

TEXT_MUTED = (150, 154, 163)
TEXT_WHITE = (255, 255, 255)
RED = (246, 70, 93)
GREEN = (14, 203, 129)
BLUE = (59, 130, 246)
DIVIDER = (40, 42, 48)
LOGO_BLOCK_BG = (24, 26, 31)


def font(weight: int, size: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT_PATH, size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


def new_card(height: int) -> Image.Image:
    """Порожня картка CARD_W x height із заокругленими кутами."""
    card = Image.new("RGB", (CARD_W, height), (0, 0, 0))
    mask = Image.new("L", (CARD_W, height), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, CARD_W, height], radius=CARD_RADIUS, fill=255)
    card.paste(Image.new("RGB", (CARD_W, height), BG_COLOR), (0, 0), mask)
    return card


def paste_logo_area(card: Image.Image, logo_path: Optional[str], box, crop_center=(0.5, 0.5)) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    if not logo_path or not os.path.exists(logo_path):
        card.paste(Image.new("RGB", (w, h), LOGO_BLOCK_BG), (x0, y0))
        return

    logo = Image.open(logo_path).convert("RGBA")
    fitted = ImageOps.fit(logo, (w, h), method=Image.LANCZOS, centering=crop_center)

    mask_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    ImageDraw.Draw(mask_layer).rounded_rectangle(box, radius=18, fill=(255, 255, 255, 255))

    layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    layer.paste(fitted, (x0, y0))

    card.paste(layer, (0, 0), mask_layer)


def to_png_buffer(card: Image.Image, name: str, output_path: Optional[str] = None) -> io.BytesIO:
    """PNG у пам'яті (для send_photo); output_path — додатково зберегти на диск."""
    if output_path:
        card.save(output_path, "PNG")
    buffer = io.BytesIO()
    card.save(buffer, "PNG")
    buffer.seek(0)
    buffer.name = name
    return buffer
