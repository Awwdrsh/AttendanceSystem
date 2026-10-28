"""
Chart geometry for the SVG chart components (components/charts/Charts.tsx and Kpi.tsx TrendBars). Each function
returns a plain dict that a template in templates/components/charts/ draws; no JavaScript is needed except the hover
tooltips, which read the `title` of each mark.

Series colours follow charts/palette.ts: a fixed categorical order, slot 1 is CoreValley hydro green. Light and dark
steps are CSS variables (--hj-viz-1 ... --hj-viz-8) so the same SVG works in both schemes.
"""
from __future__ import annotations

import math
from typing import Any, Callable, Sequence

from . import format as fmt

W = 640
SERIES_VARS = [f'var(--hj-viz-{i})' for i in range(1, 9)]


def _color(s: dict, k: int) -> str:
    return s.get('color') or SERIES_VARS[k % len(SERIES_VARS)]


def _r(x: float) -> float:
    return round(x, 2)


def column_chart(labels: Sequence[str], series: Sequence[dict], *, height: int = 200, stacked: bool = False,
                 money: bool = False, format: Callable[[float], str] | None = None, aria_label: str = '') -> dict:
    """series: [{'key', 'label', 'values': [number|None], 'color'?}]"""
    left, right, top, bottom = 40, 6, 10, 24
    n = len(labels)
    f = format or ((lambda v: f'Rs {fmt.group_digits(str(round(v)), "lakh")}') if money else (lambda v: fmt.fmt_num(v)))
    sums = [sum((x['values'][i] or 0) for x in series) if stacked else max([0, *[(x['values'][i] or 0) for x in series]])
            for i in range(n)]
    max_v, ticks = fmt.nice_scale(max([0, *sums]))
    plot_h = height - top - bottom
    slot = (W - left - right) / max(n, 1)
    group_w = min(slot * 0.7, 46)
    bar_w = group_w if stacked else max(2, (group_w - (len(series) - 1) * 2) / max(len(series), 1))
    y = lambda v: top + plot_h - (v / max_v) * plot_h  # noqa: E731
    every = math.ceil(n / 12) if n else 1
    groups = []
    for i, label in enumerate(labels):
        gx = left + slot * i + (slot - group_w) / 2
        bars = []
        for k, s in enumerate(series):
            v = s['values'][i] or 0
            if v <= 0:
                continue
            base = sum(max(0, o['values'][i] or 0) for o in series[:k]) if stacked else 0
            x = gx if stacked else gx + k * (bar_w + 2)
            y_top = y(base + v)
            h = max(1, y(base) - y_top - (2 if base > 0 else 0))
            r = min(4, bar_w / 2, h)
            is_top = not stacked or all((o['values'][i] or 0) <= 0 for o in series[k + 1:])
            d = (f'M{_r(x)},{_r(y_top + h)} V{_r(y_top + r)} Q{_r(x)},{_r(y_top)} {_r(x + r)},{_r(y_top)} '
                 f'H{_r(x + bar_w - r)} Q{_r(x + bar_w)},{_r(y_top)} {_r(x + bar_w)},{_r(y_top + r)} V{_r(y_top + h)} Z')
            bars.append({'d': d if is_top else None, 'x': _r(x), 'y': _r(y_top), 'w': _r(bar_w), 'h': _r(h),
                         'color': _color(s, k), 'title': f'{label} · {s["label"]}: {f(v)}'})
        groups.append({'bars': bars, 'label': label if i % every == 0 else None, 'lx': _r(left + slot * i + slot / 2),
                       'hx': _r(left + slot * i), 'hw': _r(slot)})
    return {'w': W, 'h': height, 'top': top, 'plot_h': plot_h, 'left': left, 'right_x': W - right, 'label_y': height - 8,
            'ticks': [{'y': _r(y(t)), 'ty': _r(y(t) + 3), 'label': fmt.compact(t, money), 'zero': t == 0} for t in ticks],
            'groups': groups, 'legend': [{'label': s['label'], 'color': _color(s, k)} for k, s in enumerate(series)],
            'aria_label': aria_label}


def line_chart(labels: Sequence[str], series: Sequence[dict], *, height: int = 200, suffix: str = '',
               format: Callable[[float], str] | None = None, aria_label: str = '', min_zero: bool = True) -> dict:
    left, right, top, bottom = 40, 10, 12, 24
    n = len(labels)
    f = format or (lambda v: f'{fmt.fmt_num(v)}{suffix}')
    allv = [v for s in series for v in s['values'] if v is not None]
    max_v, ticks = fmt.nice_scale(max([0 if min_zero else min([*allv, 0]), *allv, 0]))
    plot_h = height - top - bottom
    slot = (W - left - right) / max(n, 1)
    x = lambda i: left + slot * i + slot / 2  # noqa: E731
    y = lambda v: top + plot_h - (v / max_v) * plot_h  # noqa: E731
    every = math.ceil(n / 12) if n else 1
    lines = []
    for k, s in enumerate(series):
        segs, cur = [], ''
        for i, v in enumerate(s['values']):
            if v is None:
                if cur:
                    segs.append(cur.strip())
                cur = ''
            else:
                cur += f'{"L" if cur else "M"}{_r(x(i))},{_r(y(v))} '
        if cur:
            segs.append(cur.strip())
        points = [{'cx': _r(x(i)), 'cy': _r(y(v)), 'title': f'{labels[i]} · {s["label"]}: {f(v)}'}
                  for i, v in enumerate(s['values']) if v is not None]
        lines.append({'segments': segs, 'points': points, 'color': _color(s, k)})
    return {'w': W, 'h': height, 'left': left, 'right_x': W - right, 'label_y': height - 8, 'aria_label': aria_label,
            'ticks': [{'y': _r(y(t)), 'ty': _r(y(t) + 3), 'label': f'{fmt.compact(t)}{suffix if suffix and t != 0 else ""}',
                       'zero': t == 0} for t in ticks],
            'lines': lines, 'labels': [{'x': _r(x(i)), 'label': l} for i, l in enumerate(labels) if i % every == 0],
            'legend': [{'label': s['label'], 'color': _color(s, k)} for k, s in enumerate(series)]}


def donut(data: Sequence[dict], *, size: int = 160, center_label: str = '', aria_label: str = '') -> dict:
    """data: [{'label', 'count'}]"""
    total = sum(d['count'] for d in data)
    r, stroke = 60, 18
    circ = 2 * math.pi * r
    gap = 2 if len([d for d in data if d['count'] > 0]) > 1 else 0
    offset, segs, legend = 0.0, [], []
    for i, d in enumerate(data):
        color = SERIES_VARS[i % len(SERIES_VARS)]
        legend.append({'label': d['label'], 'count': d['count'], 'color': color,
                       'pct': f'{fmt.round_half_up(d["count"] / total * 100)}%' if total else fmt.DASH})
        if not d['count'] or not total:
            continue
        ln = d['count'] / total * circ
        segs.append({'dash': f'{_r(max(0, ln - gap))} {_r(circ)}', 'offset': _r(-offset), 'color': color,
                     'title': f'{d["label"]}: {d["count"]}'})
        offset += ln
    return {'size': size, 'r': r, 'stroke': stroke, 'segments': segs, 'legend': legend, 'total': total,
            'center_label': center_label, 'aria_label': aria_label}


def hbars(data: Sequence[dict], *, format: Callable[[float], str] | None = None, color: str | None = None,
          max_rows: int = 10) -> dict:
    """data: [{'label', 'value'}]: ranked categories with the value at the end of each bar."""
    rows = list(data)[:max_rows]
    mx = max([1, *[d['value'] for d in rows]])
    f = format or fmt.fmt_num
    return {'rows': [{'label': d['label'], 'value': f(d['value']), 'pct': _r(max(1.5, d['value'] / mx * 100))} for d in rows],
            'color': color or SERIES_VARS[0], 'more': max(0, len(data) - max_rows)}


def trend_bars(points: Sequence[dict], *, label: str, max: float = 100, suffix: str = '%', height: int = 160) -> dict:
    """Daily bars on a 0-100 scale; days with no value (weekends, holidays) are a short grey tick (Kpi.tsx TrendBars)."""
    w, h, top, bottom, left = 720, height, 8, 22, 34
    plot_h = h - top - bottom
    n = len(points) or 1
    bw = (w - left) / n
    values = [p['value'] for p in points if p['value'] is not None]
    avg = sum(values) / len(values) if values else None
    ticks = [t * max / 100 for t in (0, 50, 100)]
    bars = []
    for i, p in enumerate(points):
        x = left + i * bw + bw * 0.18
        inner = _r(max_(2, bw * 0.64))
        d = fmt.fmt_date(p['date'])
        if p['value'] is None:
            bars.append({'x': _r(x), 'y': _r(top + plot_h - 2), 'w': inner, 'h': 2, 'empty': True, 'title': f'{d}: no working day'})
        else:
            bh = max_(2, plot_h * p['value'] / max)
            bars.append({'x': _r(x), 'y': _r(top + plot_h - bh), 'w': inner, 'h': _r(bh), 'empty': False,
                         'last': i == len(points) - 1, 'title': f'{d}: {fmt.fmt_num(p["value"])}{suffix}'})
    step = math.ceil(n / 6)
    labels = [{'x': _r(left + i * bw + bw / 2), 'label': fmt.fmt_date(p['date']).rsplit(' ', 1)[0]}
              for i, p in enumerate(points) if i % step == 0 or i == n - 1]
    return {'w': w, 'h': h, 'left': left, 'label_y': h - 4, 'label': label, 'suffix': suffix,
            'range': f'{fmt.fmt_date(points[0]["date"])} to {fmt.fmt_date(points[-1]["date"])}' if points else 'No data yet',
            'avg': None if avg is None else f'{fmt.round_half_up(avg)}{suffix}',
            'ticks': [{'y': _r(top + plot_h - plot_h * t / max), 'ty': _r(top + plot_h - plot_h * t / max + 4),
                       'label': f'{fmt.fmt_num(int(t) if float(t).is_integer() else t)}{suffix}'} for t in ticks],
            'bars': bars, 'labels': labels}


def max_(a: float, b: float) -> float:
    return a if a > b else b


def sparkline(values: Sequence[float | None], *, width: int = 120, height: int = 28) -> dict[str, Any]:
    vals = [v for v in values if v is not None]
    if not vals:
        return {'w': width, 'h': height, 'd': ''}
    lo, hi = min(vals), max(vals)
    span = (hi - lo) or 1
    n = max(len(values) - 1, 1)
    d, pen = '', False
    for i, v in enumerate(values):
        if v is None:
            pen = False
            continue
        px, py = _r(i / n * (width - 2) + 1), _r(height - 2 - (v - lo) / span * (height - 4))
        d += f'{"L" if pen else "M"}{px},{py} '
        pen = True
    return {'w': width, 'h': height, 'd': d.strip()}
