"""
The day as a ribbon (components/ArrivalsRibbon.tsx): every clock-in is a mark on a time axis with the shift start drawn
as a band, marks stacking upward when people arrive within the same few minutes.

    from hajiri.punch.ribbon import arrivals_ribbon
    ctx['ribbon'] = arrivals_ribbon([{'id', 'name', 'at': datetime | ISO | None, 'late': bool}], shift_start='10:00', grace=15)
    {% include "punch/partials/arrivals_ribbon.html" with r=ribbon %}
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

ROW_H = 11


def _minute_of_day(at) -> int:
    if isinstance(at, str):
        at = datetime.fromisoformat(at.replace('Z', '+00:00'))
    d = at.astimezone(timezone.utc) + timedelta(minutes=345)
    return d.hour * 60 + d.minute


def _hhmm(m: int) -> str:
    return f'{m // 60:02d}:{m % 60:02d}'


def arrivals_ribbon(arrivals: list[dict], shift_start: str = '10:00', grace: int = 15) -> dict:
    sh, sm = (int(x) for x in shift_start.split(':')[:2])
    start = sh * 60 + sm
    times = [_minute_of_day(a['at']) for a in arrivals if a.get('at')]
    lo = math.floor((min([start - 60, *times]) - 10) / 30) * 30
    hi = math.ceil((max([start + 60, *times]) + 10) / 30) * 30
    span = hi - lo

    def pos(m: float) -> float:
        return (min(max(m, lo), hi) - lo) / span * 100
    bucket = max(2, round((hi - lo) / 60))
    stacks: dict[int, int] = {}
    marks = []
    for a in arrivals:
        if not a.get('at'):
            continue
        m = _minute_of_day(a['at'])
        b = m // bucket
        level = stacks.get(b, 0)
        stacks[b] = level + 1
        marks.append({'name': a['name'], 'late': bool(a.get('late')), 'left': pos(b * bucket), 'bottom': 4 + level * ROW_H,
                      'tip': f"{a['name']}, {_hhmm(m)}{' (late)' if a.get('late') else ''}", 'level': level})
    hours = [{'label': _hhmm(t), 'left': pos(t)} for t in range(math.ceil(lo / 30) * 30, hi + 1, 30)]
    max_level = max([3, *[m['level'] + 1 for m in marks]])
    return {'marks': marks, 'hours': hours, 'height': max_level * ROW_H + 8, 'start_left': pos(start), 'grace_width': grace / span * 100,
            'shift_start': shift_start}
