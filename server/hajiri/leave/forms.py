"""
Form posts turned into the TS request bodies the services validate (hajiri.core.schema mirrors the zod schemas, so the
same limits and messages apply to the pages and the JSON API).
"""
from __future__ import annotations

from hajiri.core.errors import ValidationError


def _num(v: str, path: str):
    try:
        f = float(v)
    except ValueError:
        raise ValidationError([{'path': path, 'message': 'Expected number, received string'}])
    return int(f) if f.is_integer() else f


def _on(post, key: str) -> bool:
    return post.get(key) in ('on', 'true', '1')


def apply_body(post, employee_id: str) -> dict:
    """LeaveApplyForm: type, dates, half day or hours, reason and attachment reference."""
    b: dict = {'employeeId': employee_id, 'leaveTypeId': post.get('leave_type_id') or '', 'from': post.get('from') or '',
               'to': post.get('to') or post.get('from') or ''}
    hourly = _on(post, 'hourly') and b['from'] == b['to']
    if _on(post, 'half') and not hourly:
        b['halfDay'] = post.get('half_which') or 'first_half'
    if hourly:
        b['fromTime'] = post.get('from_time') or ''
        b['toTime'] = post.get('to_time') or ''
    if (post.get('reason') or '').strip():
        b['reason'] = post['reason']
    if (post.get('attachment') or '').strip():
        b['attachmentKey'] = post['attachment']
    if _on(post, 'override'):
        b['override'] = True
    return b


def preview_query(get) -> dict | None:
    """The preview's query, or None while the form is incomplete (no type, no dates, to before from, bad hours)."""
    t, frm, to = get.get('leave_type_id'), get.get('from'), get.get('to') or get.get('from')
    if not t or not frm or not to or to < frm:
        return None
    q = {'leaveTypeId': t, 'from': frm, 'to': to}
    if get.get('employee_id'):
        q['employeeId'] = get['employee_id']
    hourly = _on(get, 'hourly') and frm == to
    if hourly:
        ft, tt = get.get('from_time') or '', get.get('to_time') or ''
        if not (ft < tt):
            return None
        q.update({'fromTime': ft, 'toTime': tt})
    elif _on(get, 'half'):
        q['halfDay'] = get.get('half_which') or 'first_half'
    return q


def adjust_body(post) -> dict:
    """LeaveAdjustPage: employee, type, days, kind, effective date and reason."""
    b: dict = {'employeeId': post.get('employee_id') or '', 'leaveTypeId': post.get('leave_type_id') or '',
               'reason': (post.get('reason') or ''), 'kind': post.get('kind') or 'adjustment'}
    b['deltaDays'] = _num(post.get('delta_days') or '0', 'deltaDays')
    if post.get('effective_date'):
        b['effectiveDate'] = post['effective_date']
    return b


NUMBER_FIELDS = ('accrualDays', 'accrualPerWorkedDays', 'maxBalance', 'expiryDays', 'negativeBalanceLimit',
                 'certificateAfterDays', 'maxConsecutiveDays', 'minServiceDays')
SWITCHES = ('paid', 'allowHalfDay', 'allowHourly', 'sandwichRule', 'carryForward', 'encashable')
LOCKED = ('accrualBasis', 'accrualDays', 'accrualPerWorkedDays', 'maxBalance', 'paid', 'genderScope', 'expiryDays')


def type_body(post, *, locked: bool, partial: bool) -> dict:
    """LeaveTypesPage save(): empty values are left out; a statutory type never sends its entitlement fields."""
    b: dict = {}
    for k in ('code', 'nameEn', 'nameNe', 'accrualBasis', 'genderScope'):
        v = post.get(k)
        if v not in (None, ''):
            b[k] = v.upper() if k == 'code' else v
    for k in NUMBER_FIELDS:
        v = (post.get(k) or '').strip()
        if v != '':
            b[k] = _num(v, k)
    for k in SWITCHES:
        b[k] = _on(post, k)
    if not b.get('paid', True):
        b['allowHourly'] = False
    club = post.get('clubWithId')
    b['clubWithId'] = club or None
    if partial:
        b.pop('code', None)  # the code cannot change once a type exists
    if locked:
        for k in LOCKED:
            b.pop(k, None)
    return b
