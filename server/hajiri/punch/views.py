"""Work from home check-ins (/attendance/from-home, RemoteCheckinsPage.tsx): every clock-in from home with its photo."""
from __future__ import annotations

from datetime import date, timedelta

from django.shortcuts import render
from django.views.decorators.http import require_GET

from hajiri.web.decorators import require_role
from hajiri.web.format import fmt_date_long, fmt_time
from hajiri.web.htmx import htmx_target
from hajiri.web.nav import area

from . import services
from .forms import FromHomeFilterForm


def _dir_label(d: str) -> str:
    return 'Clocked in' if d == 'in' else 'Clocked out' if d == 'out' else 'Punch'


@require_GET
@require_role('manager', *area('attendance'))
def from_home(request):
    today = services.local_date_of(services.to_ms(services._now()))
    f = FromHomeFilterForm(request.GET, initial={'date': today}).values()
    to = min(str(f['date']), today)
    span, direction = f['span'], f['dir']
    from_ = (date.fromisoformat(to) - timedelta(days=6)).isoformat() if span == 'week' else to
    everything = services.remote(request.hajiri_user, {'from': from_, 'to': to})['punches']
    shown = [p for p in everything if p['direction'] == 'in'] if direction == 'in' else everything
    by_day: dict[str, list] = {}
    for p in shown:
        p['dir_label'] = _dir_label(p['direction'])
        p['time'] = fmt_time(p['punched_at'])
        p['has_photo'] = bool(p['photo_id'] and not p['photo_purged'])
        p['location_text'] = (f"Location shared{', within ' + str(round(p['accuracy_m'])) + ' m' if p['accuracy_m'] else ''}"
                              if p['location_shared'] else 'No location shared')
        p['modal_title'] = f"{p['name_en']}, {p['dir_label'].lower()} {p['time']} on {fmt_date_long(p['local_date'])}"
        by_day.setdefault(p['local_date'], []).append(p)
    ins = [p for p in everything if p['direction'] == 'in']
    stats = [
        {'label': 'People from home', 'value': len({p['employee_id'] for p in everything})},
        {'label': 'Clock-ins with photo', 'value': sum(1 for p in ins if p['photo_id'] and not p['photo_purged'])},
        {'label': 'Without location', 'value': sum(1 for p in ins if not p['location_shared'])},
    ]
    ctx = {'date': to, 'today': today, 'span': span, 'dir': direction, 'stats': stats,
           'days': [{'date': d, 'label': fmt_date_long(d), 'punches': ps} for d, ps in by_day.items()],
           'empty_title': 'Nobody clocked in from home' if span == 'day' else 'No home clock-ins this week'}
    tpl = 'punch/partials/from_home.html' if htmx_target(request) == 'from-home' else 'punch/from_home.html'
    return render(request, tpl, ctx)
