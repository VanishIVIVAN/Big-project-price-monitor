"""Server-side black-and-white SVG price chart.

Stores are told apart by line style and marker shape, not by color."""
from __future__ import annotations

from datetime import datetime, timedelta
from html import escape

from .domain import format_money

DASHES = ("", "7 4", "2 3", "9 3 2 3", "1 4")
MARKERS = ("circle", "square", "triangle", "diamond", "cross")


def _marker(kind: str, x: float, y: float) -> str:
    style = 'fill="#fff" stroke="#000" stroke-width="1.5"'
    if kind == "circle":
        return f'<circle cx="{x:.1f}" cy="{y:.1f}" r="3.5" {style}/>'
    if kind == "square":
        return f'<rect x="{x - 3.5:.1f}" y="{y - 3.5:.1f}" width="7" height="7" {style}/>'
    if kind == "triangle":
        pts = f"{x:.1f},{y - 4.5:.1f} {x + 4.5:.1f},{y + 3.5:.1f} {x - 4.5:.1f},{y + 3.5:.1f}"
        return f'<polygon points="{pts}" {style}/>'
    if kind == "diamond":
        pts = f"{x:.1f},{y - 4.5:.1f} {x + 4.5:.1f},{y:.1f} {x:.1f},{y + 4.5:.1f} {x - 4.5:.1f},{y:.1f}"
        return f'<polygon points="{pts}" {style}/>'
    return (f'<path d="M{x - 4:.1f} {y:.1f}H{x + 4:.1f}M{x:.1f} {y - 4:.1f}V{y + 4:.1f}" '
            'stroke="#000" stroke-width="1.5" fill="none"/>')


def render_price_chart(series: dict[str, list[tuple[datetime, int]]],
                       width: int = 720, height: int = 300) -> str:
    """Return an inline SVG with one line per store, or '' if there is no data."""
    points = [p for pts in series.values() for p in pts]
    if not points:
        return ""
    left, right, top, bottom = 76, 16, 14, 30
    legend_row = 20
    total_height = height + legend_row * len(series) + 8
    plot_w, plot_h = width - left - right, height - top - bottom

    t_min = min(t for t, _ in points)
    t_max = max(t for t, _ in points)
    if t_min == t_max:
        t_min, t_max = t_min - timedelta(hours=12), t_max + timedelta(hours=12)
    span = (t_max - t_min).total_seconds()
    p_min, p_max = min(p for _, p in points), max(p for _, p in points)
    pad = max((p_max - p_min) * 0.12, p_max * 0.01)
    low, high = p_min - pad, p_max + pad

    def x_of(t: datetime) -> float:
        return left + (t - t_min).total_seconds() / span * plot_w

    def y_of(p: float) -> float:
        return top + (high - p) / (high - low) * plot_h

    out = [f'<svg viewBox="0 0 {width} {total_height}" width="100%" role="img" '
           'aria-label="Графік цін по магазинах" xmlns="http://www.w3.org/2000/svg" '
           'font-family="inherit" font-size="12">']
    for i in range(5):
        value = low + (high - low) * i / 4
        y = y_of(value)
        out.append(f'<line x1="{left}" y1="{y:.1f}" x2="{width - right}" y2="{y:.1f}" '
                   'stroke="#bbb" stroke-width="1"/>')
        out.append(f'<text x="{left - 8}" y="{y + 4:.1f}" text-anchor="end">'
                   f'{escape(format_money(round(value)))}</text>')
    fmt = "%d.%m %H:%M" if span < 2 * 86400 else "%d.%m.%Y"
    for i, anchor in ((0, "start"), (1, "middle"), (2, "end")):
        t = t_min + (t_max - t_min) * i / 2
        out.append(f'<text x="{x_of(t):.1f}" y="{height - 8}" text-anchor="{anchor}">'
                   f'{t.astimezone().strftime(fmt)}</text>')
    out.append(f'<line x1="{left}" y1="{top}" x2="{left}" y2="{top + plot_h}" stroke="#000"/>')
    out.append(f'<line x1="{left}" y1="{top + plot_h}" x2="{width - right}" '
               f'y2="{top + plot_h}" stroke="#000"/>')

    for index, (name, pts) in enumerate(sorted(series.items())):
        dash, marker = DASHES[index % len(DASHES)], MARKERS[index % len(MARKERS)]
        coords = [(x_of(t), y_of(p)) for t, p in pts]
        if len(coords) > 1:
            path = " ".join(f"{x:.1f},{y:.1f}" for x, y in coords)
            out.append(f'<polyline points="{path}" fill="none" stroke="#000" '
                       f'stroke-width="1.5" stroke-dasharray="{dash}"/>')
        out.extend(_marker(marker, x, y) for x, y in coords)
        ly = height + legend_row * index + 12
        out.append(f'<line x1="{left}" y1="{ly}" x2="{left + 36}" y2="{ly}" stroke="#000" '
                   f'stroke-width="1.5" stroke-dasharray="{dash}"/>')
        out.append(_marker(marker, left + 18, ly))
        out.append(f'<text x="{left + 46}" y="{ly + 4}">{escape(name)}</text>')
    out.append("</svg>")
    return "".join(out)
