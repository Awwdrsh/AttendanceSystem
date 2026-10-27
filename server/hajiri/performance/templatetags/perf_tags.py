"""Display helpers of the performance and learning pages (pages/performance/parts.tsx)."""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django import template

from hajiri.performance.common import fmt_number, js_round, label_of_rating, num
from hajiri.web.format import fmt_date_short, fmt_money, fmt_time, today_npt

register = template.Library()

PHASE_LABEL = {'draft': 'Draft', 'self_review': 'Self review', 'manager_review': 'Manager review', 'peer_review': 'Peer review',
               'calibration': 'Calibration', 'released': 'Released', 'closed': 'Closed'}
PHASE_TONE = {'draft': 'idle', 'self_review': 'info', 'manager_review': 'accent', 'peer_review': 'info', 'calibration': 'warn',
              'released': 'ok', 'closed': 'idle'}
CONFIDENCE = {1: ('Off track', 'bad'), 2: ('At risk', 'warn'), 3: ('On track', 'ok')}
ENROLL_STATUS = {'pending_approval': ('Awaiting approval', 'warn'), 'enrolled': ('Enrolled', 'info'), 'completed': ('Completed', 'ok'),
                 'rejected': ('Not approved', 'idle'), 'cancelled': ('Cancelled', 'idle'), 'no_show': ('Did not attend', 'bad')}
DELIVERY = {'online': 'Online', 'classroom': 'Classroom', 'blended': 'Blended'}
TAG = {'positive': ('Went well', 'ok'), 'improvement': ('To improve', 'warn'), 'neutral': ('Note', 'idle')}
CADENCE = {'none': 'One-off', 'weekly': 'Weekly', 'biweekly': 'Every 2 weeks', 'monthly': 'Monthly'}


@register.filter
def phase_label(p):
    return PHASE_LABEL.get(p, p)


@register.filter
def phase_tone(p):
    return PHASE_TONE.get(p, 'idle')


@register.filter
def rating_label(n, labels):
    return label_of_rating(n, labels or [])


@register.filter
def pct_round(v):
    return js_round(num(v))


@register.filter
def progress_tone(v):
    v = js_round(num(v))
    return 'ok' if v >= 100 else 'info' if v >= 60 else 'warn' if v >= 30 else 'bad'


@register.filter
def numstr(v):
    try:
        return '' if v is None or v == '' else fmt_number(v)
    except (TypeError, ValueError):
        return str(v)


@register.filter
def kr_value(k):
    if k['measure'] == 'boolean':
        return 'Done' if num(k['current_value']) >= 1 else 'Not done'
    f = (lambda x: fmt_money(str(x), currency=False)) if k['measure'] == 'currency' else (lambda x: f'{num(x):,.10g}')
    unit = '%' if k['measure'] == 'percent' else f" {k['unit']}" if k.get('unit') else ''
    return f"{f(k['current_value'])}{unit} of {f(k['target_value'])}{unit}"


@register.filter
def confidence_label(v):
    return CONFIDENCE.get(v, ('', ''))[0] if v else ''


@register.filter
def confidence_tone(v):
    return CONFIDENCE.get(v, ('', 'idle'))[1] if v else ''


@register.filter
def short_dt(v):
    """'28 Ashwin, 10:30' in Nepal time (parts.tsx fmtDateTime)."""
    if not v:
        return '—'
    return f'{fmt_date_short(v)}, {fmt_time(v)}'


@register.filter
def enroll_label(s):
    return ENROLL_STATUS.get(s, (s, 'idle'))[0]


@register.filter
def enroll_tone(s):
    return ENROLL_STATUS.get(s, (s, 'idle'))[1]


@register.filter
def delivery_label(d):
    return DELIVERY.get(d, d)


@register.filter
def tag_label(t):
    return TAG.get(t, (t, 'idle'))[0]


@register.filter
def tag_tone(t):
    return TAG.get(t, (t, 'idle'))[1]


@register.filter
def cadence_label(c):
    return CADENCE.get(c, c)


@register.filter
def expiry_state(d):
    """'past' | 'soon' | 'ok' for a certificate expiry date (CertificateExpiry)."""
    if not d:
        return ''
    iso = d.isoformat() if isinstance(d, (date, datetime)) else str(d)[:10]
    today = today_npt()
    soon = (date.fromisoformat(today) + timedelta(days=30)).isoformat()
    return 'past' if iso < today else 'soon' if iso <= soon else 'ok'


@register.filter
def capfirst_status(s):
    return (s or '')[:1].upper() + (s or '')[1:]


@register.filter
def get(d, k):
    try:
        return d.get(k) if isinstance(d, dict) else d[k]
    except (KeyError, IndexError, TypeError):
        return None


@register.filter
def ids(rows, key='id'):
    return [r[key] for r in rows]


@register.inclusion_tag('performance/partials/multi_pick.html')
def multi_pick(name, options, selected=None, label='', help='', max=0, required=False, id=None, placeholder='Search by name',
               text='name_en', extra='designation'):
    """
    Searchable multi-select (ColleaguePicker / MultiSelect). options: rows with 'id' and a label column (`text`, plus
    `extra` after a comma when set, "Sita Sharma, Engineer"); selected: ids picked already.
    """
    opts = []
    for o in options or []:
        lbl = str(o.get(text) or '')
        if extra and o.get(extra):
            lbl = f'{lbl}, {o[extra]}'
        opts.append({'v': str(o['id']), 'l': lbl})
    known = {o['v'] for o in opts}
    sel = [str(x) for x in (selected or []) if str(x) in known]
    return {'name': name, 'opts': opts, 'sel': sel, 'label': label, 'help': help, 'max': int(max or 0), 'required': required,
            'id': id or f'mp-{name}', 'placeholder': placeholder}
