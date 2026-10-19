import io
from datetime import datetime
from typing import Optional

from PIL import ImageDraw

from .card_common import (
    CARD_W,
    DIVIDER,
    GREEN,
    RED,
    TEXT_MUTED,
    TEXT_WHITE,
    font,
    new_card,
    to_png_buffer,
)


def generate_stats_card(
    *,
    total_trades: int,
    profitable_count: int,
    losing_count: int,
    total_pnl: float,
    symbol_pnl: list,
    strategy_stats: Optional[list] = None,
    account_label: Optional[str] = None,
    generated_at: Optional[datetime] = None,
    max_symbols: int = 8,
    max_strategies: int = 8,
    output_path: Optional[str] = None,
) -> io.BytesIO:
    """
    symbol_pnl: list of (symbol, pnl) tuples, any order — will be sorted by |pnl| desc.
    """
    ts = generated_at or datetime.now()
    accent = GREEN if total_pnl >= 0 else RED

    rows = sorted(symbol_pnl, key=lambda kv: abs(kv[1]), reverse=True)
    shown_rows = rows[:max_symbols]
    rest = rows[max_symbols:]
    if rest:
        rest_sum = sum(v for _, v in rest)
        shown_rows = shown_rows + [(f"Інші ({len(rest)})", rest_sum)]

    # --- підготовка даних по стратегіях (визначає додаткову висоту картки) ---
    shown_strats: list = []
    if strategy_stats:
        strat_rows = sorted(strategy_stats, key=lambda s: (s[1] + s[2]), reverse=True)
        shown_strats = strat_rows[:max_strategies]
        strat_rest = strat_rows[max_strategies:]
        if strat_rest:
            rest_win = sum(s[1] for s in strat_rest)
            rest_loss = sum(s[2] for s in strat_rest)
            shown_strats = shown_strats + [(f"Інші ({len(strat_rest)})", rest_win, rest_loss)]

    pad = 40

    # Layout constants — used both to size the card and to draw it, so they can never
    # get out of sync (no risk of content being clipped or trailing empty space).
    TOP = 36
    TITLE_ADV = 40          # "Статистика угод" label
    PNL_LABEL_ADV = 32      # "СУМАРНИЙ PnL" label
    PNL_BIG_ADV = 78        # big total-pnl number
    GAP_AFTER_BIG = 20
    STATS_COL_H = 66
    GAP_AFTER_STATS = 30
    DIVIDER_ADV = 1 + 32
    LIST_LABEL_ADV = 24 + 14
    ROW_H = 46
    END_GAP = 24 + 1 + 24   # gap + divider + gap
    STRAT_ROW_H = 44
    STRAT_END_GAP = 24 + 1 + 24  # gap + divider + gap, only counted when section is shown
    FOOTER_ADV = 20
    BOTTOM_PAD = 32

    row_count = max(1, len(shown_rows))

    strat_block_h = 0
    if shown_strats:
        strat_block_h = LIST_LABEL_ADV + len(shown_strats) * STRAT_ROW_H + STRAT_END_GAP

    card_h = (
        TOP
        + TITLE_ADV
        + PNL_LABEL_ADV
        + PNL_BIG_ADV
        + GAP_AFTER_BIG
        + STATS_COL_H
        + GAP_AFTER_STATS
        + DIVIDER_ADV
        + LIST_LABEL_ADV
        + row_count * ROW_H
        + END_GAP
        + strat_block_h
        + FOOTER_ADV
        + BOTTOM_PAD
    )

    card = new_card(card_h)

    draw = ImageDraw.Draw(card)

    y = TOP

    f_label = font(500, 22)
    draw.text((pad, y), "Статистика угод", font=f_label, fill=TEXT_MUTED)
    y += TITLE_ADV

    f_pnl_label = font(500, 20)
    draw.text((pad, y), "СУМАРНИЙ PnL", font=f_pnl_label, fill=TEXT_MUTED)
    y += PNL_LABEL_ADV

    f_pnl_big = font(800, 56)
    pnl_text = f"{total_pnl:+.2f} USDT"
    draw.text((pad, y), pnl_text, font=f_pnl_big, fill=accent)
    y += PNL_BIG_ADV

    y += GAP_AFTER_BIG

    # --- три колонки: угод / прибуткових / збиткових ---
    col_w = (CARD_W - pad * 2) / 3
    stats = [
        ("Угод", str(total_trades), TEXT_WHITE),
        ("Прибуткових", str(profitable_count), GREEN),
        ("Збиткових", str(losing_count), RED),
    ]

    f_stat_label = font(500, 16)
    f_stat_val = font(800, 34)

    stats_top = y
    for i, (label, val, color) in enumerate(stats):
        cx = pad + col_w * i
        draw.text((cx, stats_top), label, font=f_stat_label, fill=TEXT_MUTED)
        draw.text((cx, stats_top + 26), val, font=f_stat_val, fill=color)
        if i < 2:
            line_x = pad + col_w * (i + 1) - 24
            draw.line([(line_x, stats_top), (line_x, stats_top + STATS_COL_H)], fill=DIVIDER, width=1)

    y = stats_top + STATS_COL_H + GAP_AFTER_STATS

    draw.line([(pad, y), (CARD_W - pad, y)], fill=DIVIDER, width=1)
    y += 32

    f_list_label = font(500, 20)
    draw.text((pad, y), "PnL по монетах", font=f_list_label, fill=TEXT_MUTED)
    y += LIST_LABEL_ADV

    if shown_rows:
        max_abs = max(abs(v) for _, v in shown_rows) or 1.0
        label_w = 150
        value_w = 130
        bar_x0 = pad + label_w
        bar_max_w = CARD_W - pad - value_w - bar_x0

        f_row_symbol = font(600, 19)
        f_row_val = font(700, 19)

        for symbol, pnl in shown_rows:
            row_color = GREEN if pnl >= 0 else RED
            symbol_disp = symbol.upper().replace("-", "")

            text_y = y + ROW_H / 2 - 11
            draw.text((pad, text_y), symbol_disp, font=f_row_symbol, fill=TEXT_WHITE)

            bar_h = 18
            bar_y = y + ROW_H / 2 - bar_h / 2
            bar_w = max(4, bar_max_w * (abs(pnl) / max_abs))
            draw.rounded_rectangle(
                [bar_x0, bar_y, bar_x0 + bar_w, bar_y + bar_h],
                radius=6,
                fill=row_color,
            )

            val_text = f"{pnl:+.2f}"
            vw = draw.textlength(val_text, font=f_row_val)
            draw.text((CARD_W - pad - vw, text_y), val_text, font=f_row_val, fill=row_color)

            y += ROW_H
    else:
        f_empty = font(500, 18)
        draw.text((pad, y), "Немає закритих угод", font=f_empty, fill=TEXT_MUTED)
        y += ROW_H

    y += 24
    draw.line([(pad, y), (CARD_W - pad, y)], fill=DIVIDER, width=1)
    y += 24

    # --- Win/Loss по стратегіях (опційна секція) ---
    if shown_strats:
        f_strat_title = font(500, 20)
        draw.text((pad, y), "Win/Loss по стратегіях", font=f_strat_title, fill=TEXT_MUTED)
        y += LIST_LABEL_ADV

        name_w = 260
        value_col_w = 110  # місце під "12W / 4L" праворуч
        f_strat_name = font(600, 19)
        f_strat_val = font(700, 18)

        for strat_name, win, loss in shown_strats:
            total = win + loss
            win_ratio = (win / total) if total else 0.0

            text_y = y + STRAT_ROW_H / 2 - 11
            draw.text((pad, text_y), strat_name, font=f_strat_name, fill=TEXT_WHITE)

            bar_x0 = pad + name_w
            bar_max_w = CARD_W - pad - value_col_w - bar_x0
            bar_h = 18
            bar_y = y + STRAT_ROW_H / 2 - bar_h / 2

            # трек (програшна частина, червоний фон) + зелений сегмент (виграшна частина) поверх
            draw.rounded_rectangle(
                [bar_x0, bar_y, bar_x0 + bar_max_w, bar_y + bar_h], radius=6, fill=RED
            )
            win_w = max(0, bar_max_w * win_ratio)
            if win_w > 0:
                draw.rounded_rectangle(
                    [bar_x0, bar_y, bar_x0 + win_w, bar_y + bar_h], radius=6, fill=GREEN
                )

            val_text = f"{win}W / {loss}L"
            vw = draw.textlength(val_text, font=f_strat_val)
            draw.text((CARD_W - pad - vw, text_y), val_text, font=f_strat_val, fill=TEXT_MUTED)

            y += STRAT_ROW_H

        y += 24
        draw.line([(pad, y), (CARD_W - pad, y)], fill=DIVIDER, width=1)
        y += 24

    f_date = font(400, 16)
    footer_text = ts.strftime("%d.%m.%Y %H:%M")
    if account_label:
        footer_text = f"{account_label}  ·  {footer_text}"
    draw.text((pad, y), footer_text, font=f_date, fill=TEXT_MUTED)

    return to_png_buffer(card, "stats_card.png", output_path)
