import io
import os
from datetime import datetime
from typing import Optional

import qrcode
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


def _font(weight: int, size: int) -> ImageFont.FreeTypeFont:
    f = ImageFont.truetype(FONT_PATH, size)
    try:
        f.set_variation_by_axes([weight])
    except Exception:
        pass
    return f


def _paste_logo_area(card: Image.Image, logo_path: Optional[str], box, crop_center=(0.5, 0.5)) -> None:
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0

    if not logo_path or not os.path.exists(logo_path):
        placeholder = Image.new("RGB", (w, h), LOGO_BLOCK_BG)
        card.paste(placeholder, (x0, y0))
        return

    logo = Image.open(logo_path).convert("RGBA")
    fitted = ImageOps.fit(logo, (w, h), method=Image.LANCZOS, centering=crop_center)

    mask_layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    mask_draw = ImageDraw.Draw(mask_layer)
    mask_draw.rounded_rectangle(box, radius=18, fill=(255, 255, 255, 255))

    layer = Image.new("RGBA", card.size, (0, 0, 0, 0))
    layer.paste(fitted, (x0, y0))

    card.paste(layer, (0, 0), mask_layer)


def _price_decimals(*values: float) -> int:
    ref = 0.0
    for v in values:
        if v:
            ref = abs(v)
            break
    if ref >= 1:
        return 4
    elif ref >= 0.01:
        return 6
    return 8


def generate_pnl_card(
    *,
    symbol: str,
    side: str,
    leverage: int,
    account_label: str,
    card_type: str = "closed",
    roe_percent: Optional[float] = None,
    entry_price: Optional[float] = None,
    close_price: Optional[float] = None,
    margin_usdt: Optional[float] = None,
    stop_loss_price: Optional[float] = None,
    take_profit_summary: Optional[str] = None,
    closed_at: Optional[datetime] = None,
    referral_code: Optional[str] = None,
    logo_path: Optional[str] = None,
    logo_crop_center: tuple = (0.5, 0.5),
    price_decimals: Optional[int] = None,
    output_path: Optional[str] = None,
) -> io.BytesIO:
    is_opened = card_type == "opened"
    ts = closed_at or datetime.now()

    if price_decimals is None:
        price_decimals = _price_decimals(entry_price or 0.0, close_price or 0.0)

    if is_opened:
        accent = BLUE
    else:
        accent = GREEN if (roe_percent or 0.0) >= 0 else RED

    side_label = "Довга" if side.upper() == "LONG" else "Коротка"

    card = Image.new("RGB", (CARD_W, CARD_H), (0, 0, 0))
    mask = Image.new("L", (CARD_W, CARD_H), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, CARD_W, CARD_H], radius=CARD_RADIUS, fill=255)
    bg = Image.new("RGB", (CARD_W, CARD_H), BG_COLOR)
    card.paste(bg, (0, 0), mask)

    draw = ImageDraw.Draw(card)

    pad = 40
    y = 36

    f_label = _font(500, 22)
    top_label = "Позицію відкрито" if is_opened else "Позицію закрито"
    draw.text((pad, y), top_label, font=f_label, fill=TEXT_MUTED)
    y += 40

    f_title = _font(800, 30)
    f_side = _font(700, 30)

    title_x = pad
    symbol_disp = symbol.upper().replace("-", "")
    draw.text((title_x, y), symbol_disp, font=f_title, fill=TEXT_WHITE)
    title_x += draw.textlength(symbol_disp, font=f_title)

    sep = "  |  "
    draw.text((title_x, y + 2), sep, font=f_side, fill=TEXT_MUTED)
    title_x += draw.textlength(sep, font=f_side)

    draw.text((title_x, y + 2), side_label, font=f_side, fill=accent)
    title_x += draw.textlength(side_label, font=f_side)

    lev_txt = f"  {leverage}X"
    draw.text((title_x, y + 2), lev_txt, font=f_side, fill=TEXT_WHITE)

    y += 70

    logo_box = (pad, y, CARD_W - pad, y + 220)
    _paste_logo_area(card, logo_path, logo_box, crop_center=logo_crop_center)
    y += 220 + 30

    f_kv_label = _font(500, 20)
    f_kv_val = _font(600, 20)

    if is_opened:
        f_big_label = _font(500, 20)
        draw.text((pad, y), "МАРЖА", font=f_big_label, fill=TEXT_MUTED)
        y += 32

        f_big = _font(800, 56)
        margin_text = f"${margin_usdt:,.2f}" if margin_usdt is not None else "—"
        draw.text((pad, y), margin_text, font=f_big, fill=accent)
        y += 78

        entry_str = f"{entry_price:.{price_decimals}f}" if entry_price else "—"
        draw.text((pad, y), "Ціна входу", font=f_kv_label, fill=TEXT_MUTED)
        draw.text((pad + 260, y), entry_str, font=f_kv_val, fill=TEXT_WHITE)
        y += 32

        if stop_loss_price:
            sl_str = f"{stop_loss_price:.{price_decimals}f}"
            draw.text((pad, y), "Стоп-лосс", font=f_kv_label, fill=TEXT_MUTED)
            draw.text((pad + 260, y), sl_str, font=f_kv_val, fill=RED)
            y += 32

        if take_profit_summary:
            draw.text((pad, y), "Тейк-профіт", font=f_kv_label, fill=TEXT_MUTED)
            draw.text((pad + 260, y), take_profit_summary, font=f_kv_val, fill=GREEN)
            y += 32

        y += 12
    else:
        f_roi_label = _font(500, 20)
        draw.text((pad, y), "ROI", font=f_roi_label, fill=TEXT_MUTED)
        y += 32

        f_roi = _font(800, 56)
        roi_text = f"{(roe_percent or 0.0):+.2f}%"
        draw.text((pad, y), roi_text, font=f_roi, fill=accent)
        y += 78

        close_str = f"{close_price:.{price_decimals}f}" if close_price else "—"
        entry_str = f"{entry_price:.{price_decimals}f}" if entry_price else "—"

        draw.text((pad, y), "Ціна закриття", font=f_kv_label, fill=TEXT_MUTED)
        draw.text((pad + 260, y), close_str, font=f_kv_val, fill=TEXT_WHITE)
        y += 32
        draw.text((pad, y), "Ціна входу", font=f_kv_label, fill=TEXT_MUTED)
        draw.text((pad + 260, y), entry_str, font=f_kv_val, fill=TEXT_WHITE)
        y += 44

    draw.line([(pad, y), (CARD_W - pad, y)], fill=DIVIDER, width=1)
    y += 32

    avatar_d = 44
    avatar_box = (pad, y, pad + avatar_d, y + avatar_d)
    draw.ellipse(avatar_box, fill=(45, 48, 56))
    f_avatar_glyph = _font(700, 20)
    glyph = "B"
    gw = draw.textlength(glyph, font=f_avatar_glyph)
    draw.text((pad + avatar_d / 2 - gw / 2, y + 10), glyph, font=f_avatar_glyph, fill=TEXT_WHITE)

    f_acc = _font(600, 18)
    f_date = _font(400, 16)
    draw.text((pad + avatar_d + 14, y + 2), account_label, font=f_acc, fill=TEXT_WHITE)
    draw.text((pad + avatar_d + 14, y + 24), ts.strftime("%m-%d %H:%M"), font=f_date, fill=TEXT_MUTED)

    if referral_code:
        f_ref_label = _font(400, 15)
        f_ref_val = _font(700, 20)

        qr_size = 64
        qr_x = CARD_W - pad - qr_size
        qr_y = y - 10

        qr_img = qrcode.make(referral_code).resize((qr_size, qr_size))
        card.paste(qr_img.convert("RGB"), (qr_x, qr_y))

        text_right_x = qr_x - 16
        ref_label = "Реферальний код"
        ref_val = referral_code

        lw = draw.textlength(ref_label, font=f_ref_label)
        vw = draw.textlength(ref_val, font=f_ref_val)

        draw.text((text_right_x - lw, y), ref_label, font=f_ref_label, fill=TEXT_MUTED)
        draw.text((text_right_x - vw, y + 20), ref_val, font=f_ref_val, fill=TEXT_WHITE)

    if output_path:
        card.save(output_path, "PNG")

    buffer = io.BytesIO()
    card.save(buffer, "PNG")
    buffer.seek(0)
    buffer.name = f"{symbol.replace('-', '')}_{card_type}.png"
    return buffer


# if __name__ == "__main__":
#     buf_closed = generate_pnl_card(
#         symbol="VINE-USDT",
#         side="SHORT",
#         leverage=20,
#         card_type="closed",
#         roe_percent=-2.36,
#         entry_price=0.008138,
#         close_price=0.008148,
#         account_label="mi***s@gmail...",
#         closed_at=datetime(2026, 8, 7, 23, 13),
#         referral_code="C2E79H",
#         logo_path="/home/claude/user_logo.jpg",
#         logo_crop_center=(0.5, 0.28),
#         output_path="/home/claude/pnl_card_closed_preview.png",
#     )
#     print(f"closed: {len(buf_closed.getvalue())} bytes")

#     buf_opened = generate_pnl_card(
#         symbol="VINE-USDT",
#         side="SHORT",
#         leverage=20,
#         card_type="opened",
#         entry_price=0.008138,
#         margin_usdt=4.99,
#         stop_loss_price=0.008320,
#         take_profit_summary="2 рівні",
#         account_label="mi***s@gmail...",
#         closed_at=datetime(2026, 8, 7, 23, 13),
#         referral_code="C2E79H",
#         logo_path="/home/claude/user_logo.jpg",
#         logo_crop_center=(0.5, 0.28),
#         output_path="/home/claude/pnl_card_opened_preview.png",
#     )
#     print(f"opened: {len(buf_opened.getvalue())} bytes")