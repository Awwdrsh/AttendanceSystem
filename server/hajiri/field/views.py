"""
Field pages:
- /attendance/visits (FieldVisitsPage.tsx: visits and breaks, CSV), /attendance/kiosks (KiosksPage.tsx)
- /settings/locations (LocationsPage.tsx: locations, rules, staff)
- /me/attendance/roster (MyRosterPage.tsx: roster and shift swaps), /me/attendance/visits (MyFieldVisitsPage.tsx),
  /me/attendance/kiosk (MyKioskPage.tsx: kiosk PIN and the rotating QR badge)
- the Home card for breaks and field visits (HomeFieldCard.tsx): GET /_/field/home, load it with hx-trigger="load".
"""
from __future__ import annotations

from datetime import date, datetime, timedelta

from django.http import HttpResponse
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from engines.attendance import local_date_of

from hajiri.attendance.repo import to_ms
from hajiri.core.auth import company_id_of
from hajiri.core.clock import now as _now
from hajiri.core.db import rows
from hajiri.core.errors import HttpError
from hajiri.web.csv import csv_response
from hajiri.web.decorators import login_required, require_feature, require_module, require_role
from hajiri.web.format import fmt_date_long, fmt_date_short, fmt_time, minutes_to_hm, relative_time
from hajiri.web.htmx import close_modal, htmx_target, refresh, toast
from hajiri.web.nav import area

from . import services

ROLES = ('manager', *area('attendance'))
MODE_HINT = {
    'off': 'Locations are not checked. Anyone can clock in from the browser anywhere.',
    'flag': 'Clocking in away from a location needs a short reason and is flagged for the manager.',
    'block': 'Clocking in away from a location is refused. Field staff and people working from home are not affected.',
}
SWAP_STATUS = {'awaiting_colleague': ('Waiting for colleague', 'warn'), 'pending': ('Waiting for manager', 'info'),
               'approved': ('Approved', 'ok'), 'rejected': ('Not approved', 'bad'), 'declined': ('Declined', 'idle'),
               'cancelled': ('Cancelled', 'idle')}


def osm_link(lat: float, lng: float, zoom: int = 17) -> str:
    return f'https://www.openstreetmap.org/?mlat={lat:.6f}&mlon={lng:.6f}#map={zoom}/{lat:.6f}/{lng:.6f}'


def _today() -> str:
    return local_date_of(to_ms(_now()))


def _iso(v: str | None, default: str) -> str:
    try:
        return date.fromisoformat(v).isoformat() if v else default
    except ValueError:
        return default


def _add(iso: str, n: int) -> str:
    return (date.fromisoformat(iso) + timedelta(days=n)).isoformat()


def _since(iso_or_dt, now: datetime) -> int:
    return max(0, int((now - iso_or_dt).total_seconds() // 60))


def _bool(v) -> bool:
    return v in ('on', 'true', '1', True)


def _err(request, e: HttpError, title: str | None = None) -> HttpResponse:
    return toast(request, HttpResponse(status=e.status), e.message, tone='err', title=title)


def _csv(out: dict) -> HttpResponse:
    return csv_response(f"{out['filename']}.csv", out['columns'], out['rows'])


# ================================================================ /attendance/visits

@require_GET
@require_role(*ROLES)
@require_feature('field')
def visits(request):
    user = request.hajiri_user
    t = _today()
    view = 'breaks' if request.GET.get('view') == 'breaks' else 'visits'
    d = min(_iso(request.GET.get('date'), t), t)
    span = 'week' if request.GET.get('span') == 'week' else 'day'
    over = request.GET.get('over') == '1'
    from_ = _add(d, -6) if span == 'week' else d
    q = {'from': from_, 'to': d}
    if request.GET.get('export') == 'csv':
        out = (services.list_visits if view == 'visits' else services.list_breaks)(user, {**q, 'format': 'csv'})
        return _csv(out)
    ctx = {'view': view, 'date': d, 'today': t, 'span': span, 'over': over, 'from': from_}
    if view == 'visits':
        v = services.list_visits(user, q)['visits']
        for x in v:
            x['day'] = fmt_date_short(x['local_date'])
            x['times'] = fmt_time(x['started_at'])
            x['ended'] = fmt_time(x['ended_at']) if x['ended_at'] else None
            x['arrival_url'] = osm_link(x['start_lat'], x['start_lng']) if x['start_lat'] is not None and x['start_lng'] is not None else None
            x['departure_url'] = osm_link(x['end_lat'], x['end_lng']) if x['end_lat'] is not None and x['end_lng'] is not None else None
            x['modal_title'] = f"{x['name_en']} at {x['place']}, {fmt_time(x['started_at'])} on {fmt_date_long(x['local_date'])}"
        ctx.update(rows=v, stats=[
            {'label': 'People in the field', 'value': len({x['employee_id'] for x in v})}, {'label': 'Visits', 'value': len(v)},
            {'label': 'In progress', 'value': sum(1 for x in v if not x['ended_at'])},
            {'label': 'Time on visits', 'value': minutes_to_hm(sum(x['minutes'] for x in v))}])
    else:
        b = services.list_breaks(user, {**q, **({'overOnly': '1'} if over else {})})['breaks']
        for x in b:
            x['day'] = fmt_date_short(x['local_date'])
            x['label'] = services.BREAK_LABEL.get(x['kind'], x['kind'])
            x['ended'] = fmt_time(x['ended_at']) if x['ended_at'] else 'now'
        ctx.update(rows=b, stats=[
            {'label': 'Breaks', 'value': len(b)}, {'label': 'Total time', 'value': minutes_to_hm(sum(x['minutes'] or 0 for x in b))},
            {'label': 'Over limit', 'value': sum(1 for x in b if x['over_limit']), **({'tone': 'warn'} if any(x['over_limit'] for x in b) else {})},
            {'label': 'On a break now', 'value': sum(1 for x in b if x['open'])}])
    tpl = 'field/partials/visits.html' if htmx_target(request) == 'field-visits' else 'field/visits.html'
    return render(request, tpl, ctx)


@require_GET
@login_required
@require_feature('field')
def visit_photo(request, id):
    from hajiri.punch.api import photo_response
    return photo_response(services.visit_photo(request.hajiri_user, str(id)))


# ================================================================ /attendance/kiosks

def _kiosk_status(k: dict) -> tuple[str, str]:
    if k['status'] == 'revoked':
        return 'Revoked', 'idle'
    if k['locked']:
        return 'Paused', 'bad'
    if k['status'] == 'pairing':
        return 'Waiting to pair', 'warn'
    online = k['last_seen_at'] and (_now() - k['last_seen_at']).total_seconds() < 600
    return ('Online', 'ok') if online else ('Idle', 'idle')


def _kiosk_rows(request) -> list[dict]:
    ks = services.kiosks(request.hajiri_user)['kiosks']
    for k in ks:
        k['status_label'], k['status_tone'] = _kiosk_status(k)
        k['place'] = ' · '.join(x for x in (k['branch_name'], k['location_name']) if x) or 'Any branch'
        k['seen'] = relative_time(k['last_seen_at']) if k['last_seen_at'] else 'Never'
        k['paired'] = fmt_date_short(k['paired_at']) if k['paired_at'] else '—'
    return ks


@require_GET
@require_module('attendance')
@require_feature('field')
def kiosks(request):
    ctx = {'kiosks': _kiosk_rows(request), 'kiosk_url': request.build_absolute_uri('/kiosk')}
    tpl = 'field/partials/kiosks.html' if htmx_target(request) == 'kiosks' else 'field/kiosks.html'
    return render(request, tpl, ctx)


def _branches(company_id: str) -> list[dict]:
    return [dict(b, id=str(b['id'])) for b in rows('SELECT id, name_en FROM branch WHERE company_id = %s ORDER BY name_en', [company_id])]


def _locations(company_id: str) -> list[dict]:
    return [dict(x, id=str(x['id'])) for x in rows('SELECT id, name FROM location WHERE company_id = %s ORDER BY active DESC, name', [company_id])]


def _kiosk_body(p) -> dict:
    return {'name': (p.get('name') or '').strip(), 'branchId': p.get('branch_id') or None, 'locationId': p.get('location_id') or None,
            'selfieRequired': _bool(p.get('selfie_required'))}


def _pairing_fragment(request, name: str, pairing: dict) -> HttpResponse:
    return render(request, 'field/partials/pairing_code.html', {'name': name, 'code': pairing['pairingCode'],
                                                                'expires': fmt_time(pairing['expiresAt']),
                                                                'kiosk_url': request.build_absolute_uri('/kiosk')})


@require_http_methods(['GET', 'POST'])
@require_module('attendance')
@require_feature('field')
def kiosk_form(request, id=None):
    user = request.hajiri_user
    company_id = company_id_of(user)
    if request.method == 'GET':
        k = services.kiosk_detail(user, str(id)) if id else {'name': '', 'branch_id': None, 'location_id': None, 'selfie_required': True}
        return render(request, 'field/partials/kiosk_form.html', {'k': k, 'id': id, 'branches': _branches(company_id),
                                                                   'locations': _locations(company_id)})
    body = _kiosk_body(request.POST)
    if id:
        services.update_kiosk(user, str(id), body)
        return toast(request, refresh(close_modal(HttpResponse(status=204)), 'kiosks'), 'Kiosk saved.')
    r = services.create_kiosk(user, body)
    return refresh(_pairing_fragment(request, body['name'], r), 'kiosks')


@require_POST
@require_module('attendance')
@require_feature('field')
def kiosk_action(request, id, action):
    user = request.hajiri_user
    kid = str(id)
    if action == 'pairing-code':
        name = services.kiosk_detail(user, kid)['name']
        return refresh(_pairing_fragment(request, name, services.kiosk_pairing_code(user, kid)), 'kiosks')
    if action == 'unlock':
        services.unlock_kiosk(user, kid)
        return toast(request, refresh(HttpResponse(status=204), 'kiosks'), 'Kiosk unpaused.')
    if action == 'revoke':
        services.revoke_kiosk(user, kid)
        return toast(request, refresh(HttpResponse(status=204), 'kiosks'), 'Kiosk revoked.')
    from hajiri.core.errors import BadRequestError
    raise BadRequestError('unknown action')


# ================================================================ /settings/locations

def _locations_ctx(request) -> dict:
    user = request.hajiri_user
    locs = services.locations(user)['locations']
    for x in locs:
        x['map_url'] = osm_link(x['lat'], x['lng'])
    return {'locations': locs}


def _staff_ctx(request) -> dict:
    user = request.hajiri_user
    flt = 'all' if request.GET.get('staff') == 'all' else 'field'
    locs = services.locations(user)['locations']
    people = [s for s in services.staff(user)['staff'] if flt == 'all' or s['field_work'] or s['location_ids']]
    for s in people:
        s['placeholder'] = f"{s['branch_name']} locations" if s['branch_name'] else 'Branch locations'
    return {'staff': people, 'staff_filter': flt, 'loc_options': [{'id': x['id'], 'name': x['name']} for x in locs]}


@require_GET
@require_role('hr')
def locations(request):
    target = htmx_target(request)
    if target == 'locations-table':
        return render(request, 'field/partials/locations_table.html', _locations_ctx(request))
    if target == 'field-staff':
        return render(request, 'field/partials/staff.html', _staff_ctx(request))
    policy = services.field_policy(company_id_of(request.hajiri_user))
    ctx = {**_locations_ctx(request), **_staff_ctx(request), 'policy': policy, 'mode_hint': MODE_HINT,
           'kinds': [(k, services.BREAK_LABEL[k], policy['breaks'][k]) for k in services.BREAK_KINDS]}
    return render(request, 'field/locations.html', ctx)


def _float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return v


def _int(v, default=None):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return default


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def location_form(request, id=None):
    user = request.hajiri_user
    company_id = company_id_of(user)
    if request.method == 'GET':
        loc = next((x for x in services.locations(user)['locations'] if x['id'] == str(id)), None) if id else None
        if id and not loc:
            from hajiri.core.errors import NotFoundError
            raise NotFoundError('location not found')
        return render(request, 'field/partials/location_form.html', {
            'loc': loc or {'name': '', 'address': '', 'branch_id': None, 'lat': '', 'lng': '', 'radius_m': 150, 'active': True},
            'id': id, 'branches': _branches(company_id)})
    p = request.POST
    body = {'name': (p.get('name') or '').strip(), 'address': (p.get('address') or '').strip() or None, 'branchId': p.get('branch_id') or None,
            'lat': _float(p.get('lat')), 'lng': _float(p.get('lng')), 'radiusM': _int(p.get('radius_m'), 150) or 150,
            'active': _bool(p.get('active'))}
    if id:
        services.update_location(user, str(id), body)
    else:
        services.create_location(user, body)
    resp = refresh(refresh(close_modal(HttpResponse(status=204)), 'locations-table'), 'field-staff')
    return toast(request, resp, 'Location saved.' if id else 'Location added.')


@require_POST
@require_role('hr')
def location_delete(request, id):
    services.delete_location(request.hajiri_user, str(id))
    resp = refresh(refresh(HttpResponse(status=204), 'locations-table'), 'field-staff')
    return toast(request, resp, 'Location deleted.')


@require_POST
@require_role('hr')
def field_rules(request):
    p = request.POST
    body = {'geofenceMode': p.get('geofenceMode') or 'flag',
            'breaks': {k: {'enabled': _bool(p.get(f'{k}_enabled')), 'paid': _bool(p.get(f'{k}_paid')),
                           'maxMin': _int(p.get(f'{k}_maxMin'), 5) or 5} for k in services.BREAK_KINDS},
            'fieldVisitClockIn': _bool(p.get('fieldVisitClockIn')), 'fieldPhotoRequired': _bool(p.get('fieldPhotoRequired'))}
    services.put_policy(request.hajiri_user, body)
    return toast(request, HttpResponse(status=204), 'Rules saved.')


@require_POST
@require_role('hr')
def staff_update(request, employee_id):
    p = request.POST
    body = {}
    if 'field_work' in p or p.get('which') == 'field':
        body['fieldWork'] = _bool(p.get('field_work'))
    if p.get('which') == 'locations':
        body['locationIds'] = [x for x in p.getlist('location_ids') if x]
    try:
        services.update_staff(request.hajiri_user, str(employee_id), body)
    except HttpError as e:
        return refresh(_err(request, e), 'field-staff')
    return HttpResponse(status=204)


@require_POST
@require_role('hr')
def staff_kiosk_reset(request, employee_id):
    eid = str(employee_id)
    name = next((s['name_en'] for s in services.staff(request.hajiri_user)['staff'] if s['id'] == eid), '')
    services.reset_kiosk_pin(request.hajiri_user, eid)
    return toast(request, refresh(HttpResponse(status=204), 'field-staff'),
                 f'Kiosk PIN reset for {name}. They can set a new one in the app.')


# ================================================================ /me/attendance/roster

def _shift_text(code, name) -> str:
    return (name or code) if code else 'Off'


def _day_line(d: dict) -> tuple[str, bool]:
    if d['shift']:
        s = d['shift']
        return f"{s['name_en'] or s['code']}, {s['start']} to {s['end']}", False
    if d['source'] == 'weekly_off':
        return 'Weekly off', True
    if d['source'] == 'roster_off':
        return 'Off (rostered)', True
    return 'No shift', True


@require_GET
@login_required
def my_roster(request):
    user = request.hajiri_user
    weeks = request.GET.get('weeks') if request.GET.get('weeks') in ('1', '2', '4') else '2'
    t = _today()
    days = []
    swaps_in, swaps_other = [], []
    if user.employee_id:
        r = services.my_roster(user, {'from': t, 'to': _add(t, int(weeks) * 7 - 1)})
        for d in r['days']:
            text, muted = _day_line(d)
            days.append({**d, 'label': fmt_date_long(d['date']) + (' (today)' if d['date'] == t else ''), 'today': d['date'] == t,
                         'text': text, 'muted': muted})
        for s in services.swaps(user, {})['swaps']:
            s['mine'] = s['requester_id'] == user.employee_id
            s['status_label'], s['status_tone'] = SWAP_STATUS.get(s['status'], (s['status'], 'idle'))
            s['req_shift'] = _shift_text(s['requester_shift_code'], s['requester_shift_name'])
            s['col_shift'] = _shift_text(s['colleague_shift_code'], s['colleague_shift_name'])
            s['date_long'] = fmt_date_long(s['local_date'])
            s['date_short'] = fmt_date_short(s['local_date'])
            s['can_cancel'] = s['mine'] and s['status'] in ('awaiting_colleague', 'pending')
            (swaps_in if s['colleague_id'] == user.employee_id and s['status'] == 'awaiting_colleague' else swaps_other).append(s)
    ctx = {'weeks': weeks, 'days': days, 'incoming': swaps_in, 'others': swaps_other}
    tpl = 'field/partials/my_roster.html' if htmx_target(request) == 'my-roster' else 'field/my_roster.html'
    return render(request, tpl, ctx)


@require_http_methods(['GET', 'POST'])
@login_required
def swap_new(request):
    user = request.hajiri_user
    t = _today()
    if request.method == 'GET':
        d = _iso(request.GET.get('date'), _add(t, 1))
        opts = services.swap_colleagues(user, {'date': d})
        mine = opts['mine']['shift']
        return render(request, 'field/partials/swap_form.html', {
            'date': d, 'min': t, 'max': _add(t, 90), 'reason': request.GET.get('reason', ''),
            'mine': f"{mine['name_en'] or mine['code']}, {mine['start']} to {mine['end']}" if mine else 'an off day',
            'colleagues': [{'id': c['id'], 'label': f"{c['name_en']} ({(c['shift']['name_en'] or c['shift']['code']) + ' ' + c['shift']['start'] + '–' + c['shift']['end'] if c['shift'] else 'off'})"}
                           for c in opts['colleagues'] if not c['sameAsMine']]})
    p = request.POST
    body = {'colleagueId': p.get('colleague_id', ''), 'localDate': p.get('date', ''),
            **({'reason': p['reason'].strip()} if (p.get('reason') or '').strip() else {})}
    services.request_swap(user, body)
    return toast(request, refresh(close_modal(HttpResponse(status=204)), 'my-roster'), 'Swap request sent to your colleague.')


@require_POST
@login_required
def swap_action(request, id, action):
    user = request.hajiri_user
    sid = str(id)
    if action == 'accept':
        services.accept_swap(user, sid, {})
        msg = 'Accepted. It now goes to the manager for approval.'
    elif action == 'decline':
        services.decline_swap(user, sid, {})
        msg = 'Declined.'
    elif action == 'cancel':
        services.cancel_swap(user, sid)
        msg = 'Request cancelled.'
    else:
        from hajiri.core.errors import BadRequestError
        raise BadRequestError('unknown action')
    return toast(request, refresh(HttpResponse(status=204), 'my-roster'), msg)


# ================================================================ /me/attendance/visits

@require_GET
@login_required
@require_feature('field')
def my_visits(request):
    user = request.hajiri_user
    span = '30' if request.GET.get('span') == '30' else '7'
    t = _today()
    q = {'from': _add(t, -(int(span) - 1)), 'to': t}
    v = services.list_visits(user, q)['visits']
    b = services.list_breaks(user, q)['breaks']
    field_work = True
    if user.employee_id:
        try:
            field_work = services.me(user)['fieldWork']
        except HttpError:
            field_work = True
    for x in v:
        x['day'] = fmt_date_short(x['local_date'])
        x['times'] = f"{fmt_time(x['started_at'])} to {fmt_time(x['ended_at']) if x['ended_at'] else 'now'}"
        x['map_url'] = osm_link(x['start_lat'], x['start_lng']) if x['start_lat'] is not None and x['start_lng'] is not None else None
    for x in b:
        x['day'] = fmt_date_short(x['local_date'])
        x['label'] = services.BREAK_LABEL.get(x['kind'], x['kind'])
        x['times'] = f"{fmt_time(x['started_at'])} to {fmt_time(x['ended_at']) if x['ended_at'] else 'now'}"
    stats = [{'label': 'Visits', 'value': len(v)}, {'label': 'Time on visits', 'value': minutes_to_hm(sum(x['minutes'] for x in v))},
             {'label': 'Break time', 'value': minutes_to_hm(sum(x['minutes'] or 0 for x in b))},
             {'label': 'Breaks over limit', 'value': sum(1 for x in b if x['over_limit']), **({'tone': 'warn'} if any(x['over_limit'] for x in b) else {})}]
    ctx = {'span': span, 'visits': v, 'breaks': b, 'stats': stats, 'no_field': not field_work and not v}
    tpl = 'field/partials/my_visits.html' if htmx_target(request) == 'my-visits' else 'field/my_visits.html'
    return render(request, tpl, ctx)


# ================================================================ /me/attendance/kiosk

@require_GET
@login_required
@require_feature('field')
def my_kiosk(request):
    user = request.hajiri_user
    me = services.me(user) if user.employee_id else {'kioskPin': {'set': False, 'lockedUntil': None}}
    pin = me['kioskPin']
    return render(request, 'field/my_kiosk.html', {'pin_set': pin['set'], 'locked_until': fmt_time(pin['lockedUntil']) if pin['lockedUntil'] else None})


@require_POST
@login_required
@require_feature('field')
def kiosk_pin(request):
    pin = request.POST.get('pin', '')
    if pin != request.POST.get('again', ''):
        return toast(request, HttpResponse(status=400), 'The PINs do not match', tone='err')
    services.set_kiosk_pin(request.hajiri_user, {'pin': pin})
    resp = HttpResponse(status=204)
    resp['HX-Refresh'] = 'true'
    from hajiri.web.htmx import flash
    return flash(resp, 'Kiosk PIN saved.')


@require_GET
@login_required
@require_feature('field')
def kiosk_badge(request):
    """The live QR badge; the fragment reloads itself when the code's 30-second window ends."""
    try:
        r = services.kiosk_qr(request.hajiri_user)
    except HttpError:
        return render(request, 'field/partials/kiosk_badge.html', {'error': 'The badge could not be loaded. Check your connection.'})
    resp = render(request, 'field/partials/kiosk_badge.html', {'token': r['token'], 'emp_code': r['empCode'],
                                                               'refresh_s': max(1, round((r['refreshInMs'] + 200) / 1000)),
                                                               'left': max(0, round(r['refreshInMs'] / 1000))})
    resp['Cache-Control'] = 'no-store'
    return resp


# ================================================================ Home card (HomeFieldCard.tsx)

@require_GET
@login_required
def home_card(request):
    """Breaks and field visits on Home; empty when there is nothing to do."""
    from hajiri.web.nav import feature_on
    user = request.hajiri_user
    if not user.employee_id or not feature_on(request, 'field'):
        return HttpResponse('')
    try:
        d = services.me(user)
    except HttpError:
        return HttpResponse('')
    show_breaks = d['clockedIn'] and bool(d['breakTypes'])
    if not show_breaks and not d['fieldWork'] and not d['breaks']:
        return HttpResponse('')
    now = _now()
    for b in d['breaks']:
        b['label'] = services.BREAK_LABEL.get(b['kind'], b['kind'])
        b['times'] = f"{fmt_time(b['started_at'])} to {fmt_time(b['ended_at'])}"
    break_min = sum(b['minutes'] if b['minutes'] is not None else _since(b['started_at'], now) for b in d['breaks'])
    ob = d['openBreak']
    open_break = None
    if ob:
        so_far = _since(ob['started_at'], now)
        open_break = {'over': so_far > ob['max_min'],
                      'text': f"On a {services.BREAK_LABEL[ob['kind']].lower()} break since {fmt_time(ob['started_at'])}, "
                              f"{minutes_to_hm(so_far)} so far (limit {ob['max_min']} min)"}
    ov = d['openVisit']
    open_visit = {'id': ov['id'], 'place': ov['place'], 'since': fmt_time(ov['started_at']),
                  'so_far': minutes_to_hm(_since(ov['started_at'], now))} if ov else None
    for v in d['visits']:
        v['times'] = f"{fmt_time(v['started_at'])} to {fmt_time(v['ended_at'])}" if v['ended_at'] else None
    ctx = {'d': d, 'show_breaks': show_breaks, 'show_break_card': show_breaks or bool(d['breaks']),
           'break_types': [{**t, 'label': services.BREAK_LABEL[t['kind']]} for t in d['breakTypes']],
           'break_total': minutes_to_hm(break_min) if break_min > 0 else None, 'open_break': open_break, 'open_visit': open_visit,
           'ended_breaks': [b for b in d['breaks'] if b['ended_at']], 'ended_visits': [v for v in d['visits'] if v['ended_at']],
           'first_visit_clocks_in': not d['visits'] and not d['clockedIn']}
    return render(request, 'field/partials/home_card.html', ctx)


def _geo(p) -> dict:
    out = {}
    try:
        if p.get('lat') not in (None, '') and p.get('lng') not in (None, ''):
            out = {'lat': float(p['lat']), 'lng': float(p['lng'])}
            if p.get('accuracy') not in (None, ''):
                out['accuracyM'] = round(float(p['accuracy']))
    except ValueError:
        out = {}
    return out


def _home_refresh(resp: HttpResponse) -> HttpResponse:
    return refresh(resp, 'home-field')


@require_POST
@login_required
def home_break(request, action):
    user = request.hajiri_user
    if action == 'start':
        kind = request.POST.get('kind', '')
        r = services.start_break(user, {'kind': kind})
        return toast(request, _home_refresh(HttpResponse(status=204)), f"{services.BREAK_LABEL[r['break']['kind']]} break started.")
    r = services.end_break(user)['break']
    msg = f"Break ended after {minutes_to_hm(r['minutes'] or 0)}.{' That was longer than the limit, so your manager was told.' if r['over_limit'] else ''}"
    return toast(request, _home_refresh(HttpResponse(status=204)), msg)


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('field')
def home_visit_start(request):
    user = request.hajiri_user
    if request.method == 'GET':
        d = services.me(user)
        return render(request, 'field/partials/visit_start_form.html', {'photo_required': d['fieldPhotoRequired'],
                                                                         'first_clocks_in': not d['visits'] and not d['clockedIn']})
    p = request.POST
    geo = _geo(p)
    body = {'place': (p.get('place') or '').strip(), **({'purpose': p['purpose'].strip()} if (p.get('purpose') or '').strip() else {}),
            **({'photo': p['photo']} if p.get('photo') else {}), **geo}
    try:
        r = services.start_visit(user, body, request.META.get('REMOTE_ADDR'))
    except HttpError as e:
        if isinstance(e.details, dict) and e.details.get('code') == 'photo_required':
            resp = HttpResponse(status=204)
            from hajiri.web.htmx import trigger
            trigger(resp, 'hj:visit-photo-required', True)
            return toast(request, resp, 'A photo of the place is needed to start a visit.', tone='info')
        raise
    resp = toast(request, _home_refresh(close_modal(HttpResponse(status=204))), r['message'])
    if not geo:
        toast(request, resp, 'Location was not available, so the visit was saved without it.', tone='info')
    return resp


@require_http_methods(['GET', 'POST'])
@login_required
@require_feature('field')
def home_visit_end(request, id):
    user = request.hajiri_user
    if request.method == 'GET':
        d = services.me(user)
        ov = d['openVisit'] if d['openVisit'] and d['openVisit']['id'] == str(id) else None
        return render(request, 'field/partials/visit_end_form.html', {'id': id, 'place': ov['place'] if ov else None})
    p = request.POST
    body = {**({'outcome': p['outcome'].strip()} if (p.get('outcome') or '').strip() else {}), **_geo(p)}
    v = services.end_visit(user, str(id), body)['visit']
    return toast(request, _home_refresh(close_modal(HttpResponse(status=204))), f"Visit to {v['place']} ended.")
