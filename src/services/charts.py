"""
Delilah Financial OS - Visual Financial Charts & Graph Engine.

Renders dark-mode financial visualizations in pure Python (Pillow):
1. Category spending distribution & donut/progress cards.
2. Daily cash flow & burn trends (inflow vs outflow).
3. Net worth trajectory line charts.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import datetime, timedelta
from io import BytesIO
from typing import Dict, Any, List, Optional, Tuple

from PIL import Image, ImageDraw, ImageFont

from src.core.state import DB_PATH
from src.db.migrations import apply_all


# Professional Dark-Mode Financial Palette
_BG_COLOR = (24, 25, 28, 255)         # Deep slate dark mode (#18191C)
_CARD_BG = (35, 37, 42, 255)          # Card container background (#23252A)
_TEXT_MAIN = (240, 243, 246, 255)     # Bright off-white
_TEXT_MUTED = (145, 152, 161, 255)    # Subtle gray
_BORDER_COLOR = (50, 54, 62, 255)     # Subtle border
_INFLOW_COLOR = (46, 204, 113, 255)   # Emerald Green
_OUTFLOW_COLOR = (231, 76, 60, 255)   # Coral Red
_ACCENT_BLUE = (52, 152, 219, 255)    # Electric Blue

_PALETTE = [
    (52, 152, 219, 255),   # Blue
    (46, 204, 113, 255),   # Green
    (241, 196, 15, 255),   # Yellow/Gold
    (155, 89, 182, 255),   # Purple
    (230, 126, 34, 255),   # Orange
    (26, 188, 156, 255),   # Turquoise
    (231, 76, 60, 255),    # Red
    (253, 121, 168, 255),  # Pink
    (129, 236, 236, 255),  # Cyan
    (162, 155, 254, 255),  # Lavender
]


def render_category_spending_chart(
    items: List[Tuple[str, float]],
    title: str = "Spending by Category",
    period_label: str = "Last 30 Days",
) -> bytes:
    """
    Renders a category spending breakdown card with horizontal distribution bars.
    """
    width = 750
    header_height = 80
    row_height = 42
    footer_height = 40

    total_spent = sum(amt for _, amt in items) if items else 0.0
    num_rows = min(10, len(items)) if items else 1
    height = header_height + (num_rows * row_height) + footer_height + 20

    img = Image.new("RGBA", (width, height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Card background container
    draw.rounded_rectangle([(15, 15), (width - 15, height - 15)], radius=12, fill=_CARD_BG, outline=_BORDER_COLOR, width=1)

    # Title & Subtitle
    draw.text((35, 30), f"📊 {title.upper()}", fill=_TEXT_MAIN)
    sub = f"{period_label} · Total: ${total_spent:,.2f}"
    draw.text((35, 52), sub, fill=_TEXT_MUTED)

    if not items or total_spent <= 0:
        draw.text((35, 100), "No evaluated spending recorded for this period.", fill=_TEXT_MUTED)
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()

    # Draw rows
    y = header_height + 15
    bar_x = 220
    max_bar_w = 340

    for idx, (cat_name, amt) in enumerate(items[:10]):
        pct = (amt / total_spent) if total_spent > 0 else 0.0
        color = _PALETTE[idx % len(_PALETTE)]

        # Category label
        display_name = cat_name[:20] if len(cat_name) <= 20 else cat_name[:18] + "…"
        draw.text((35, y + 6), display_name, fill=_TEXT_MAIN)

        # Bar background
        draw.rounded_rectangle([(bar_x, y + 8), (bar_x + max_bar_w, y + 22)], radius=5, fill=(50, 54, 62, 255))
        # Bar filled
        fill_w = max(4, int(max_bar_w * pct))
        draw.rounded_rectangle([(bar_x, y + 8), (bar_x + fill_w, y + 22)], radius=5, fill=color)

        # Pct & Amount
        val_str = f"${amt:,.2f} ({pct * 100:.1f}%)"
        draw.text((bar_x + max_bar_w + 15, y + 6), val_str, fill=_TEXT_MAIN)

        y += row_height

    # Footer
    draw.text((35, height - 35), "Delilah Financial OS · Autonomous Ledger Intelligence", fill=_TEXT_MUTED)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def render_cash_flow_bar_chart(
    daily_data: List[Dict[str, Any]],
    title: str = "Daily Cash Flow Trend",
    period_label: str = "Last 30 Days",
) -> bytes:
    """
    Renders daily income (green) vs expenses (red) dual-bar trend chart.
    """
    width = 800
    height = 420
    img = Image.new("RGBA", (width, height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    # Card background
    draw.rounded_rectangle([(15, 15), (width - 15, height - 15)], radius=12, fill=_CARD_BG, outline=_BORDER_COLOR, width=1)

    total_inflow = sum(d.get("inflow", 0.0) for d in daily_data)
    total_outflow = sum(d.get("outflow", 0.0) for d in daily_data)
    net_flow = total_inflow - total_outflow

    # Header
    draw.text((35, 30), f"📈 {title.upper()}", fill=_TEXT_MAIN)
    net_sign = "+" if net_flow >= 0 else ""
    sub = f"{period_label} · In: ${total_inflow:,.2f} · Out: ${total_outflow:,.2f} · Net: {net_sign}${net_flow:,.2f}"
    draw.text((35, 52), sub, fill=_TEXT_MUTED)

    if not daily_data:
        draw.text((35, 120), "No transactions recorded for this period.", fill=_TEXT_MUTED)
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()

    # Graph plotting dimensions
    plot_left = 60
    plot_right = width - 40
    plot_top = 100
    plot_bottom = height - 60
    plot_h = plot_bottom - plot_top
    plot_w = plot_right - plot_left

    # Determine max value for Y axis scaling
    max_val = max(
        max((d.get("inflow", 0.0) for d in daily_data), default=1.0),
        max((d.get("outflow", 0.0) for d in daily_data), default=1.0),
        100.0
    )

    # Draw grid lines
    for i in range(4):
        gy = plot_top + int(plot_h * (i / 3.0))
        g_val = max_val * (1.0 - (i / 3.0))
        draw.line([(plot_left, gy), (plot_right, gy)], fill=(45, 48, 55, 255), width=1)
        draw.text((20, gy - 6), f"${int(g_val)}", fill=_TEXT_MUTED)

    # Draw baseline
    draw.line([(plot_left, plot_bottom), (plot_right, plot_bottom)], fill=_BORDER_COLOR, width=2)

    # Plot bars
    num_days = len(daily_data)
    slot_w = plot_w / num_days
    bar_w = max(3, int(slot_w * 0.38))

    for idx, d in enumerate(daily_data):
        x_center = plot_left + (idx * slot_w) + (slot_w / 2.0)
        inflow = d.get("inflow", 0.0)
        outflow = d.get("outflow", 0.0)

        # Inflow bar (green, left of slot)
        if inflow > 0:
            h_in = int((inflow / max_val) * plot_h)
            draw.rectangle(
                [(x_center - bar_w - 1, plot_bottom - h_in), (x_center - 1, plot_bottom)],
                fill=_INFLOW_COLOR
            )

        # Outflow bar (red, right of slot)
        if outflow > 0:
            h_out = int((outflow / max_val) * plot_h)
            draw.rectangle(
                [(x_center + 1, plot_bottom - h_out), (x_center + bar_w + 1, plot_bottom)],
                fill=_OUTFLOW_COLOR
            )

    # Legend
    legend_y = height - 42
    draw.rectangle([(plot_left, legend_y), (plot_left + 12, legend_y + 12)], fill=_INFLOW_COLOR)
    draw.text((plot_left + 18, legend_y), "Cash Inflow", fill=_TEXT_MAIN)

    draw.rectangle([(plot_left + 120, legend_y), (plot_left + 132, legend_y + 12)], fill=_OUTFLOW_COLOR)
    draw.text((plot_left + 138, legend_y), "Cash Outflow", fill=_TEXT_MAIN)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


def render_net_worth_trend_chart(
    snapshots: List[Tuple[str, float]],
    title: str = "Net Worth Trajectory",
) -> bytes:
    """
    Renders net worth trajectory line chart over historical snapshots.
    """
    width = 800
    height = 400
    img = Image.new("RGBA", (width, height), _BG_COLOR)
    draw = ImageDraw.Draw(img)

    draw.rounded_rectangle([(15, 15), (width - 15, height - 15)], radius=12, fill=_CARD_BG, outline=_BORDER_COLOR, width=1)

    if not snapshots:
        draw.text((35, 30), f"🏛️ {title.upper()}", fill=_TEXT_MAIN)
        draw.text((35, 80), "No historical net worth snapshots recorded yet.", fill=_TEXT_MUTED)
        buf = BytesIO()
        img.save(buf, format="PNG")
        buf.seek(0)
        return buf.getvalue()

    values = [val for _, val in snapshots]
    min_v = min(values)
    max_v = max(values)
    latest_v = values[-1]
    first_v = values[0]
    delta = latest_v - first_v

    # Header
    draw.text((35, 30), f"🏛️ {title.upper()}", fill=_TEXT_MAIN)
    sign = "+" if delta >= 0 else ""
    sub = f"Current: ${latest_v:,.2f} · Trajectory: {sign}${delta:,.2f} ({len(snapshots)} points)"
    draw.text((35, 52), sub, fill=_TEXT_MUTED)

    plot_left = 80
    plot_right = width - 40
    plot_top = 100
    plot_bottom = height - 60
    plot_h = plot_bottom - plot_top
    plot_w = plot_right - plot_left

    val_range = max(1.0, max_v - min_v)

    # Grid lines
    for i in range(4):
        gy = plot_top + int(plot_h * (i / 3.0))
        g_val = max_v - (val_range * (i / 3.0))
        draw.line([(plot_left, gy), (plot_right, gy)], fill=(45, 48, 55, 255), width=1)
        draw.text((20, gy - 6), f"${int(g_val):,}", fill=_TEXT_MUTED)

    # Plot line
    points = []
    num_pts = len(snapshots)
    step = plot_w / max(1, num_pts - 1)

    for idx, (_, val) in enumerate(snapshots):
        px = plot_left + (idx * step)
        norm_y = (val - min_v) / val_range
        py = plot_bottom - (norm_y * plot_h)
        points.append((px, py))

    line_color = _INFLOW_COLOR if delta >= 0 else _OUTFLOW_COLOR
    for i in range(len(points) - 1):
        draw.line([points[i], points[i + 1]], fill=line_color, width=3)
        # Dot
        draw.ellipse([(points[i][0] - 3, points[i][1] - 3), (points[i][0] + 3, points[i][1] + 3)], fill=_TEXT_MAIN)
    # Final dot
    draw.ellipse([(points[-1][0] - 4, points[-1][1] - 4), (points[-1][0] + 4, points[-1][1] + 4)], fill=_TEXT_MAIN)

    buf = BytesIO()
    img.save(buf, format="PNG")
    buf.seek(0)
    return buf.getvalue()


# ============================================================
# High-Level Database Query & Chart Dispatchers
# ============================================================

def generate_spending_chart_for_user(
    *,
    user_id: str,
    days: int = 30,
    conn: Optional[sqlite3.Connection] = None,
) -> bytes:
    """Query user transactions and generate category breakdown chart."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("generate_spending_chart_for_user: forced isolation violation — user_id is required")

    def _execute(db_conn: sqlite3.Connection) -> bytes:
        try:
            apply_all(db_conn)
        except Exception:
            pass
        c = db_conn.cursor()
        c.execute("""
            SELECT COALESCE(category, 'Uncategorized'), SUM(amount)
            FROM transactions
            WHERE user_id = ?
              AND status = 'Evaluated'
              AND amount > 0
              AND merchant NOT LIKE '%System Balance Sync%'
              AND date >= date('now', ?)
            GROUP BY COALESCE(category, 'Uncategorized')
            ORDER BY SUM(amount) DESC
            LIMIT 10
        """, (user_id, f"-{days} days"))
        rows = [(r[0], float(r[1])) for r in c.fetchall()]
        return render_category_spending_chart(rows, title="Spending by Category", period_label=f"Last {days} Days")

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def generate_cash_flow_chart_for_user(
    *,
    user_id: str,
    days: int = 30,
    conn: Optional[sqlite3.Connection] = None,
) -> bytes:
    """Query daily cash inflows and outflows and generate trend chart."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("generate_cash_flow_chart_for_user: forced isolation violation — user_id is required")

    def _execute(db_conn: sqlite3.Connection) -> bytes:
        try:
            apply_all(db_conn)
        except Exception:
            pass
        c = db_conn.cursor()

        # Build daily map for past N days
        now_dt = datetime.now().date()
        daily_map: Dict[str, Dict[str, float]] = {}
        for i in range(days - 1, -1, -1):
            d_str = (now_dt - timedelta(days=i)).strftime("%Y-%m-%d")
            daily_map[d_str] = {"date": d_str, "inflow": 0.0, "outflow": 0.0}

        c.execute("""
            SELECT date,
                   SUM(CASE WHEN amount < 0 THEN ABS(amount) ELSE 0 END) as inflow,
                   SUM(CASE WHEN amount > 0 THEN amount ELSE 0 END) as outflow
            FROM transactions
            WHERE user_id = ?
              AND merchant NOT LIKE '%System Balance Sync%'
              AND category != 'Credit Card Bill Payment 💳'
              AND date >= date('now', ?)
            GROUP BY date
            ORDER BY date ASC
        """, (user_id, f"-{days} days"))

        for r in c.fetchall():
            d_str = str(r[0])[:10]
            if d_str in daily_map:
                daily_map[d_str]["inflow"] = float(r[1] or 0.0)
                daily_map[d_str]["outflow"] = float(r[2] or 0.0)

        daily_list = list(daily_map.values())
        return render_cash_flow_bar_chart(daily_list, title="Daily Cash Flow Trend", period_label=f"Last {days} Days")

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)


def generate_net_worth_chart_for_user(
    *,
    user_id: str,
    days: int = 90,
    conn: Optional[sqlite3.Connection] = None,
) -> bytes:
    """Query historical snapshots and generate net worth trajectory chart."""
    if not user_id or not isinstance(user_id, str):
        raise ValueError("generate_net_worth_chart_for_user: forced isolation violation — user_id is required")

    def _execute(db_conn: sqlite3.Connection) -> bytes:
        try:
            apply_all(db_conn)
        except Exception:
            pass
        c = db_conn.cursor()
        c.execute("""
            SELECT captured_at, net_worth
            FROM financial_snapshots
            WHERE user_id = ?
              AND captured_at >= datetime('now', ?)
            ORDER BY captured_at ASC
        """, (user_id, f"-{days} days"))
        rows = [(str(r[0])[:10], float(r[1])) for r in c.fetchall()]
        return render_net_worth_trend_chart(rows, title=f"Net Worth Trajectory ({days}d)")

    if conn is not None:
        return _execute(conn)
    with sqlite3.connect(DB_PATH) as db_conn:
        return _execute(db_conn)
