"""Small display helpers of the TS time pages (api/time.ts hhmmOf, parts.tsx TimesheetBadge and ProjectDot)."""
from django import template
from django.utils.html import format_html

register = template.Library()

STATUS = {
    'submitted': ('Waiting for approval', 'warn'),
    'approved': ('Approved', 'ok'),
    'rejected': ('Returned', 'bad'),
    'cancelled': ('Withdrawn', 'idle'),
}
FLAG = {
    'over': 'More time logged than attended',
    'under': 'Less time logged than attended',
    'no_attendance': 'Time logged on a day with no attendance',
}


@register.filter
def hhmm(minutes) -> str:
    """"3:05" style hours and minutes for timesheet grids."""
    if not minutes:
        return '0:00'
    m = int(minutes)
    return f'{m // 60}:{m % 60:02d}'


@register.filter
def diff_hm(minutes) -> str:
    """Logged minus attended: "+1:05", "−0:30" or "0:00"."""
    m = int(minutes or 0)
    sign = '+' if m > 0 else '−' if m < 0 else ''
    return f'{sign}{hhmm(abs(m))}'


@register.filter
def diff_off(minutes) -> bool:
    return abs(int(minutes or 0)) > 60


@register.simple_tag
def timesheet_badge(status):
    label, tone = STATUS.get(status, ('Not submitted', 'idle')) if status else ('Not submitted', 'idle')
    return format_html('<span class="hj-badge hj-tone-{}">{}</span>', tone, label)


@register.simple_tag
def project_dot(color):
    return format_html('<span aria-hidden="true" style="width:8px;height:8px;border-radius:99px;background:{};display:inline-block;flex:none"></span>',
                       color or 'var(--hj-muted)')


@register.filter
def flag_label(flag) -> str:
    return FLAG.get(flag, '')


@register.filter
def hours1(minutes) -> str:
    """(min / 60).toFixed(1) + ' h'."""
    from hajiri.timesheets.services import to_fixed
    return f'{to_fixed((minutes or 0) / 60, 1)} h'


@register.filter
def fixed2(v) -> str:
    from hajiri.timesheets.services import to_fixed
    return to_fixed(float(v or 0), 2)
