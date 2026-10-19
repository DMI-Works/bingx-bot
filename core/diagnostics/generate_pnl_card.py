import io
from datetime import datetime
from typing import Optional

import qrcode
from PIL import Image, ImageDraw

from ..common.formatting import price_decimals as default_price_decimals
from .card_common import (
    BLUE,
    CARD_H,
    CARD_W,
    DIVIDER,
    GREEN,
    RED,
    TEXT_MUTED,
    TEXT_WHITE,
    font,
    new_card,
    paste_logo_area,
    to_png_buffer,
)

def _draw_direction_arrow(
    card: Image.Image, x: float, cy: float, height: int, up: bool, color: tuple
) -> int:
    """Малює стрілку напрямку позиції (вгору = LONG, вниз = SHORT) з лівим
    краєм у x і вертикальним центром cy. Малюємо у 4x і зменшуємо — так краї
    згладжені (ImageDraw.polygon сам по собі без антиаліасингу). Без шрифтів,
    тому не залежить від того, чи є гліфи ↑↓ у Inter. Повертає ширину стрілки."""
    ss = 4
    w = max(2, int(height * 0.8))
    W, H = w * ss, height * ss
    cx = W / 2
    head_h = H * 0.5
    half_shaft = W * 0.14

    layer = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(layer).polygon(
        [
            (cx, 0), (W, head_h), (cx + half_shaft, head_h), (cx + half_shaft, H),
            (cx - half_shaft, H), (cx - half_shaft, head_h), (0, head_h),
        ],
        fill=color + (255,),
    )
    if not up:
        layer = layer.transpose(Image.FLIP_TOP_BOTTOM)
    layer = layer.resize((w, height), Image.LANCZOS)
    card.paste(layer, (int(round(x)), int(round(cy - height / 2))), layer)
    return w



def generate_pnl_card(
    *,
    symbol: str,
    side: str,
    leverage: int,
    account_label: str,
    card_type: str = "closed",
    roe_percent: Optional[float] = None,
    net_pnl: Optional[float] = None,
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
        price_decimals = default_price_decimals(entry_price or 0.0, close_price or 0.0)

    if is_opened:
        accent = BLUE
    else:
        accent = GREEN if (roe_percent or 0.0) >= 0 else RED

    is_long = side.upper() == "LONG"
    side_label = "LONG" if is_long else "SHORT"
    side_color = GREEN if is_long else RED

    card = new_card(CARD_H)

    draw = ImageDraw.Draw(card)

    pad = 40
    y = 36

    f_label = font(500, 22)
    top_label = "Позицію відкрито" if is_opened else "Позицію закрито"
    draw.text((pad, y), top_label, font=f_label, fill=TEXT_MUTED)
    y += 40

    f_title = font(800, 30)
    f_side = font(700, 30)

    title_x = pad
    symbol_disp = symbol.upper().replace("-", "")
    draw.text((title_x, y), symbol_disp, font=f_title, fill=TEXT_WHITE)
    title_x += draw.textlength(symbol_disp, font=f_title)

    sep = "  |  "
    draw.text((title_x, y + 2), sep, font=f_side, fill=TEXT_MUTED)
    title_x += draw.textlength(sep, font=f_side)

    draw.text((title_x, y + 2), side_label, font=f_side, fill=side_color)
    side_bbox = draw.textbbox((title_x, y + 2), side_label, font=f_side)
    title_x += draw.textlength(side_label, font=f_side)

    arrow_h = int((side_bbox[3] - side_bbox[1]) * 0.95)
    arrow_cy = (side_bbox[1] + side_bbox[3]) / 2
    title_x += 10
    title_x += _draw_direction_arrow(card, title_x, arrow_cy, arrow_h, is_long, side_color)

    lev_txt = f"  {leverage}X"
    draw.text((title_x, y + 2), lev_txt, font=f_side, fill=TEXT_WHITE)

    y += 70

    logo_box = (pad, y, CARD_W - pad, y + 220)
    paste_logo_area(card, logo_path, logo_box, crop_center=logo_crop_center)
    y += 220 + 30

    f_kv_label = font(500, 20)
    f_kv_val = font(600, 20)

    if is_opened:
        f_big_label = font(500, 20)
        draw.text((pad, y), "МАРЖА", font=f_big_label, fill=TEXT_MUTED)
        y += 32

        f_big = font(800, 56)
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
        f_roi_label = font(500, 20)
        draw.text((pad, y), "ROI", font=f_roi_label, fill=TEXT_MUTED)
        y += 32

        f_roi = font(800, 56)
        roi_text = f"{(roe_percent or 0.0):+.2f}%"
        draw.text((pad, y), roi_text, font=f_roi, fill=accent)

        if net_pnl is not None:
            roi_w = draw.textlength(roi_text, font=f_roi)
            f_pnl = font(700, 28)
            pnl_text = f"{net_pnl:+.2f} USDT"
            draw.text((pad + roi_w + 16, y + 22), pnl_text, font=f_pnl, fill=accent)

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
    f_avatar_glyph = font(700, 20)
    glyph = "B"
    gw = draw.textlength(glyph, font=f_avatar_glyph)
    draw.text((pad + avatar_d / 2 - gw / 2, y + 10), glyph, font=f_avatar_glyph, fill=TEXT_WHITE)

    f_acc = font(600, 18)
    f_date = font(400, 16)
    draw.text((pad + avatar_d + 14, y + 2), account_label, font=f_acc, fill=TEXT_WHITE)
    draw.text((pad + avatar_d + 14, y + 24), ts.strftime("%m-%d %H:%M"), font=f_date, fill=TEXT_MUTED)

    if referral_code:
        f_ref_label = font(400, 15)
        f_ref_val = font(700, 20)

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

    return to_png_buffer(card, f"{symbol.replace('-', '')}_{card_type}.png", output_path)
