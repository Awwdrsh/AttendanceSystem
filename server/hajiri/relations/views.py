"""
Employee relations pages and their HTMX fragments (port of apps/web/src/pages/relations, components/relations and
api/relations.ts). The Employee relations hub (/relations, HR) has six tabs; employees use /me/help/grievances,
/me/profile/conduct and /me/attendance/devices. Rules live in services.py; these views turn form posts into the TS
request bodies (forms.py) and draw the result. Everything is read in the user's active company.
"""
from __future__ import annotations

from datetime import date, timedelta

from django.http import HttpResponse
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.auth import company_id_of, has_role
from hajiri.core.errors import BadRequestError, HttpError
from hajiri.core.scoping import is_uuid
from hajiri.db.models import Branch, Designation, OrgUnit
from hajiri.files.store import stored_file_response
from hajiri.web.decorators import login_required, require_feature, require_role, signed_in
from hajiri.web.htmx import close_modal, is_htmx, refresh, toast, trigger
from hajiri.web.middleware import error_message
from hajiri.web.nav import feature_on, url_of

from . import forms as F
from . import services as S

STATUS_TONE = {'submitted': 'info', 'under_review': 'info', 'hearing_scheduled': 'accent', 'decided': 'ok', 'appealed': 'warn',
               'closed': 'idle', 'withdrawn': 'idle'}
CASE_TONE = {'open': 'info', 'show_cause': 'warn', 'responded': 'accent', 'decided': 'ok', 'withdrawn': 'idle'}
DEVICE_HR_STATUS = {'pending': ('Waiting', 'warn'), 'approved': ('Approved', 'ok'), 'rejected': ('Not approved', 'bad'),
                    'revoked': ('Removed', 'idle')}
DEVICE_MY_STATUS = {'pending': ('Waiting for HR', 'warn'), 'approved': ('Approved', 'ok'), 'rejected': ('Not approved', 'bad'),
                    'revoked': ('Removed', 'idle')}
KYE_RESPONSE = {'confirmed': ('Confirmed', 'ok'), 'changes_requested': ('Asked for changes', 'warn'), 'pending': ('Pending', 'idle')}


# ---------------------------------------------------------------- helpers

def _ok(request, message: str | None, *refreshes: str, close: bool = False, status: int = 204, body: str = '') -> HttpResponse:
    resp = HttpResponse(body, status=status)
    if close:
        close_modal(resp)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message) if message else resp


def _render_ok(request, template: str, ctx: dict, message: str | None, *refreshes: str) -> HttpResponse:
    resp = render(request, template, ctx)
    for r in refreshes:
        refresh(resp, r)
    return toast(request, resp, message) if message else resp


def _is_hr(request) -> bool:
    return has_role(request.hajiri_user, 'hr')


def _structure(company_id: str) -> dict:
    """Branches, departments and active positions of the company for the pickers."""
    def opts(qs):
        return [{'id': str(i), 'name_en': n} for i, n in qs.order_by('name_en').values_list('id', 'name_en')]
    return {
        'branches': opts(Branch.objects.for_company(company_id)),
        'units': opts(OrgUnit.objects.for_company(company_id)),
        'designations': opts(Designation.objects.for_company(company_id).filter(active=True)),
    }


def _today() -> date:
    return date.fromisoformat(S.today())


# ================================================================ grievances

def _decorate_grievance(g: dict) -> dict:
    g['category_label'] = S.GRIEVANCE_CATEGORY_LABEL.get(g['category'], g['category'])
    g['status_label'] = S.GRIEVANCE_STATUS_LABEL.get(g['status'], g['status'])
    g['status_tone'] = STATUS_TONE.get(g['status'], 'idle')
    g['outcome_label'] = S.OUTCOME_LABEL.get(g['outcome']) if g.get('outcome') else None
    return g


def _grievance_list_ctx(request, scope: str, flt: str = 'all') -> dict:
    lst = [_decorate_grievance(g) for g in S.list_grievances(request.hajiri_user, {'scope': scope})['grievances']]
    if flt == 'open':
        lst = [g for g in lst if g['status'] not in ('closed', 'withdrawn')]
    return {'grievances': lst, 'scope': scope, 'filter': flt, 'show_name': scope == 'handle'}


@require_GET
@require_role('hr')
def grievances_admin(request):
    """HR: the grievance committee and every grievance it hears (HR's own grievances and cases they are excluded from
    never show here)."""
    user = request.hajiri_user
    flt = request.GET.get('filter') if request.GET.get('filter') in ('open', 'all') else 'open'
    ctx = _grievance_list_ctx(request, 'handle', flt)
    if is_htmx(request) and request.headers.get('HX-Target') == 'grievances':
        return render(request, 'relations/partials/grievance_list.html', ctx)
    com = S.committee(user)
    ctx.update({'committee': com, 'member_ids': [m['user_id'] for m in com['members']], 'users': S.company_users(user)})
    return render(request, 'relations/grievances_admin.html', ctx)


@require_POST
@require_role('hr')
def committee_save(request):
    user = request.hajiri_user
    S.set_committee(user, F.CommitteeForm(request.POST).body())
    com = S.committee(user)
    return _render_ok(request, 'relations/partials/committee.html',
                      {'committee': com, 'member_ids': [m['user_id'] for m in com['members']], 'users': S.company_users(user)},
                      'Committee saved.')


@require_GET
@login_required
@require_feature('helpdesk')
def my_grievances(request):
    """Employee: raise and follow grievances. Committee members who are not HR also handle cases from here."""
    user = request.hajiri_user
    if is_htmx(request) and request.headers.get('HX-Target') == 'grievances':
        return render(request, 'relations/partials/grievance_list.html', _grievance_list_ctx(request, 'mine'))
    if is_htmx(request) and request.headers.get('HX-Target') == 'committee-cases':
        return render(request, 'relations/partials/grievance_list.html',
                      {**_grievance_list_ctx(request, 'handle'), 'list_id': 'committee-cases'})
    com = S.committee(user)
    ctx = _grievance_list_ctx(request, 'mine')
    ctx['is_member'] = com['isMember']
    if com['isMember']:
        ctx['handle'] = _grievance_list_ctx(request, 'handle')['grievances']
    return render(request, 'relations/my_grievances.html', ctx)


@require_http_methods(['GET', 'POST'])
@login_required
def grievance_new(request):
    if request.method == 'GET':
        return render(request, 'relations/partials/grievance_form.html', {'categories': S.GRIEVANCE_CATEGORY_LABEL.items()})
    r = S.file_grievance(request.hajiri_user, F.GrievanceForm(request.POST).body())
    resp = _ok(request, f"Grievance G-{r['number']} filed. The committee will be in touch.", 'grievances', close=True)
    # MyGrievancesPage opens the new grievance at once.
    return trigger(resp, 'hj:open-grievance', {'url': reverse('relations:grievance', args=[r['id']])})


def _thread_ctx(request, gid: str) -> dict:
    user = request.hajiri_user
    d = S.grievance_detail(user, gid)
    g = _decorate_grievance(d['grievance'])
    handler = g['role'] != 'complainant'
    ctx = {**d, 'g': g, 'handler': handler, 'is_hr': _is_hr(request), 'open': g['status'] in S.OPEN_GRIEVANCE,
           'outcomes': S.OUTCOME_LABEL.items(), 'today': S.today()}
    if handler and _is_hr(request):
        ctx['users'] = S.company_users(user)
    return ctx


def _thread(request, gid: str, message: str | None = None) -> HttpResponse:
    return _render_ok(request, 'relations/partials/grievance_thread.html', _thread_ctx(request, str(gid)), message,
                      'grievances', 'committee-cases')


@require_GET
@login_required
def grievance(request, id):
    return _thread(request, id)


@require_GET
@login_required
def grievance_file(request, id, file_id):
    f = S.grievance_file(request.hajiri_user, str(id), str(file_id))
    return stored_file_response(f, download=request.GET.get('download') == '1')


@require_POST
@login_required
def grievance_note(request, id):
    S.add_note(request.hajiri_user, str(id), F.NoteForm(request.POST).note())
    return _thread(request, id, 'Sent.')


@require_POST
@login_required
def grievance_update(request, id):
    action, body = F.GrievanceUpdateForm(request.POST).body()
    S.update_grievance(request.hajiri_user, str(id), body)
    return _thread(request, id, {'review': 'Marked as under review.', 'hearing': 'Hearing set. The employee was told.',
                                 'exclude': 'Saved.'}[action])


@require_POST
@login_required
def grievance_decide(request, id):
    S.decide_grievance(request.hajiri_user, str(id), F.GrievanceDecisionForm(request.POST).body())
    return _thread(request, id, 'Decision recorded.')


@require_POST
@login_required
def grievance_appeal(request, id):
    S.appeal_grievance(request.hajiri_user, str(id), F.AppealForm(request.POST).body())
    return _thread(request, id, 'Appeal sent to HR.')


@require_POST
@login_required
def grievance_appeal_decide(request, id):
    S.decide_appeal(request.hajiri_user, str(id), F.AppealDecisionForm(request.POST).body())
    return _thread(request, id, 'Appeal decided.')


@require_POST
@login_required
def grievance_withdraw(request, id):
    S.withdraw_grievance(request.hajiri_user, str(id))
    return _thread(request, id, 'Grievance withdrawn.')


@require_POST
@login_required
def grievance_close(request, id):
    S.close_grievance(request.hajiri_user, str(id))
    return _thread(request, id, 'Grievance closed.')


# ================================================================ disciplinary

def _decorate_case(k: dict) -> dict:
    k['category_label'] = S.CASE_CATEGORY_LABEL.get(k['category'], k['category'])
    k['status_label'] = S.CASE_STATUS_LABEL.get(k['status'], k['status'])
    k['status_tone'] = CASE_TONE.get(k['status'], 'idle')
    k['late_response'] = bool(k.get('responseDue') and k.get('respondedAt')
                              and S._iso(k['respondedAt']) > k['responseDue'])
    k['past_due'] = bool(k.get('responseDue')) and k['responseDue'] < S.today()
    return k


@require_GET
@require_role('hr')
def disciplinary(request):
    """HR: disciplinary cases from incident to decision, with show-cause notices the employee answers in self-service."""
    cases = [_decorate_case(k) for k in S.list_cases(request.hajiri_user)['cases']]
    if is_htmx(request) and request.headers.get('HX-Target') == 'cases':
        return render(request, 'relations/partials/case_list.html', {'cases': cases})
    return render(request, 'relations/disciplinary.html', {'cases': cases})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def case_new(request):
    if request.method == 'GET':
        eid = request.GET.get('employee') or None
        return render(request, 'relations/partials/case_form.html', {'employee_id': eid, 'categories': S.CASE_CATEGORY_LABEL.items(),
                                                                     'today': S.today()})
    r = S.open_case(request.hajiri_user, F.CaseForm(request.POST).body())
    resp = _ok(request, f"Case D-{r['number']} opened.", 'cases', 'relations-cases', close=True)
    return trigger(resp, 'hj:open-case', {'url': reverse('relations:case', args=[r['id']])})


def _case_ctx(request, cid: str) -> dict:
    user = request.hajiri_user
    k = _decorate_case(S.case_detail(user, cid)['case'])
    stages = S.list_stages(user)['stages']
    decided_stage = next((s for s in stages if s['id'] == k['stageId']), None)
    return {'k': k, 'stages': [s for s in stages if s['active']], 'stage_effects': {s['id']: s['effect'] for s in stages},
            'decided_stage': decided_stage, 'open': k['status'] in S.OPEN_CASE, 'today': S.today(),
            'default_due': (_today() + timedelta(days=7)).isoformat(),
            'can_force': k['status'] == 'show_cause' and bool(k['responseDue']) and k['responseDue'] >= S.today(),
            'letters_on': feature_on(request, 'documents')}


def _case(request, cid, message: str | None = None) -> HttpResponse:
    return _render_ok(request, 'relations/partials/case_detail.html', _case_ctx(request, str(cid)), message, 'cases', 'relations-cases')


@require_GET
@require_role('hr')
def case(request, id):
    return _case(request, id)


@require_POST
@require_role('hr')
def case_show_cause(request, id):
    S.show_cause(request.hajiri_user, str(id), F.ShowCauseForm(request.POST).body())
    return _case(request, id, 'Notice sent to the employee.')


@require_POST
@require_role('hr')
def case_decide(request, id):
    stages = S.stages_of(company_id_of(request.hajiri_user))

    def effect_of(stage_id: str) -> str | None:
        return stages.filter(pk=stage_id).values_list('effect', flat=True).first() if is_uuid(stage_id) else None
    S.decide_case(request.hajiri_user, str(id), F.CaseDecisionForm(request.POST).body(effect_of))
    return _case(request, id, 'Decision recorded. The employee was told.')


@require_POST
@require_role('hr')
def case_withdraw(request, id):
    S.withdraw_case(request.hajiri_user, str(id), F.ReasonForm(request.POST).body())
    return _case(request, id, 'Case withdrawn.')


@require_POST
@require_role('hr')
@require_feature('documents')
def case_letter(request, id):
    """Issue the stage's letter (or a warning letter) under Documents and link it to the case (CaseDetail issueLetter)."""
    from hajiri.documents import services as docs
    user = request.hajiri_user
    ctx = _case_ctx(request, str(id))
    k, st = ctx['k'], ctx['decided_stage']
    code = (st or {}).get('letter_template_code') or 'warning'
    tpl = next((t for t in docs.list_templates(user) if t['code'] == code), None)
    if not tpl:
        raise BadRequestError(f'No "{code}" letter template. Add one under Employees, Documents & letters.')
    letter = docs.issue_letter(user, {'templateId': str(tpl['id']), 'employeeId': k['employeeId'],
                                      'purpose': f"{k['category_label'].lower()} (case D-{k['number']})", 'allowMissing': True})
    S.link_letter(user, str(id), {'letterId': letter['id']})
    return _case(request, id, 'Letter issued and linked to the case.')


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def stages(request):
    user = request.hajiri_user
    if request.method == 'GET':
        return render(request, 'relations/partials/stages.html', {'stages': S.list_stages(user)['stages'], 'slots': range(F.STAGE_SLOTS)})
    S.save_stages(user, F.StagesForm(request.POST).body())
    return _ok(request, 'Stages saved.', close=True)


@require_GET
@login_required
def conduct(request):
    """The employee's side of disciplinary cases: notices to answer and outcomes. Investigations still with HR do not show."""
    if not request.hajiri_user.employee_id:
        return redirect('/me/profile')
    cases = [_decorate_case(k) for k in S.my_cases(request.hajiri_user)['cases']]
    if is_htmx(request) and request.headers.get('HX-Target') == 'my-cases':
        return render(request, 'relations/partials/my_cases.html', {'cases': cases})
    return render(request, 'relations/conduct.html', {'cases': cases})


@require_POST
@login_required
def case_respond(request, id):
    S.respond_case(request.hajiri_user, str(id), F.CaseResponseForm(request.POST).body())
    return _ok(request, 'Your response was sent to HR.', 'my-cases', 'relations-home')


# ================================================================ milestones and contract

@require_GET
@require_role('hr')
def milestones(request):
    """HR: what is coming up in people's careers: service anniversaries, contract ends, retirements, probation ends and exits."""
    days = request.GET.get('days') if request.GET.get('days') in ('30', '60', '180', '366') else '60'
    data = S.list_milestones(request.hajiri_user, {'days': days})
    lst = data['milestones']
    for m in lst:
        m['kind_label'], m['kind_tone'] = S.MILESTONE_KIND[m['kind']]
        m['past'] = m['date'] < S.today()

    def count(k):
        return sum(1 for m in lst if m['kind'] == k)
    stats = [{'label': 'Service anniversaries', 'value': count('service')}, {'label': 'Contracts ending', 'value': count('contract_end')},
             {'label': 'Retirements', 'value': count('retirement')}, {'label': 'Leaving', 'value': count('exit')}]
    return render(request, 'relations/milestones.html', {'milestones': lst, 'days': days, 'stats': stats,
                                                         'ranges': [('30', '30 days'), ('60', '60 days'), ('180', '6 months'), ('366', '1 year')]})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def policy(request):
    user = request.hajiri_user
    pol = S.get_policy(user)['policy']
    if request.method == 'GET':
        return render(request, 'relations/partials/policy_form.html', {'policy': pol, 'years': ', '.join(str(y) for y in pol['serviceYears'])})
    S.save_policy(user, F.PolicyForm(request.POST).body(pol))
    resp = _ok(request, 'Saved.', close=True)
    resp.headers['HX-Refresh'] = 'true'
    return resp


@require_POST
@require_role('hr')
def devices_policy(request):
    user = request.hajiri_user
    pol = S.get_policy(user)['policy']
    nxt = F.DevicesPolicyForm(request.POST).cleaned()['registeredDevicesOnly']
    S.save_policy(user, {**pol, 'registeredDevicesOnly': nxt})
    resp = _ok(request, 'Clock-in now needs a registered device.' if nxt else 'Any device can clock in again.')
    resp.headers['HX-Refresh'] = 'true'
    return resp


# ================================================================ acting and deputation

def _decorate_assignment(a: dict) -> dict:
    a['where'] = S.assignment_where(a)
    a['status_label'] = {'active': 'Current', 'ended': 'Ended', 'cancelled': 'Cancelled'}.get(a['status'], a['status'])
    return a


@require_GET
@require_role('hr')
def assignments(request):
    """HR: people acting in another position or deputed to another branch or department for a while."""
    status = 'all' if request.GET.get('status') == 'all' else 'active'
    lst = [_decorate_assignment(a) for a in S.list_assignments(request.hajiri_user, {'status': status})['assignments']]
    ctx = {'assignments': lst, 'status': status}
    if is_htmx(request) and request.headers.get('HX-Target') == 'assignments':
        return render(request, 'relations/partials/assignment_list.html', ctx)
    return render(request, 'relations/assignments.html', ctx)


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def assignment_new(request):
    user = request.hajiri_user
    if request.method == 'GET':
        return render(request, 'relations/partials/assignment_form.html',
                      {'employee_id': request.GET.get('employee') or None, 'today': S.today(), **_structure(company_id_of(user))})
    S.add_assignment(user, F.AssignmentForm(request.POST).body())
    return _ok(request, 'Saved.', 'assignments', 'relations-assignments', close=True)


@require_POST
@require_role('hr')
def assignment_end(request, id):
    body = F.AssignmentEndForm(request.POST).body()
    cancel = bool(body.get('cancel'))
    S.end_assignment(request.hajiri_user, str(id), body)
    return _ok(request, 'Cancelled.' if cancel else 'Ended today.', 'assignments', 'relations-assignments')


# ================================================================ Know Your Employee

def _decorate_campaign(c: dict) -> dict:
    c['pct'] = round(c['confirmed'] / c['total'] * 100) if c.get('total') else 0
    c['section_labels'] = [S.SECTION_LABEL.get(s, s) for s in c['sections']]
    return c


def _completeness_rows(request, view: str) -> tuple[list[dict], int | None]:
    data = S.completeness_report(request.hajiri_user)['employees']
    avg = round(sum(e['percent'] for e in data) / len(data)) if data else None
    out = []
    for e in data:
        if view == 'all' or e['percent'] < 100:
            e['tone'] = 'bad' if e['percent'] < 60 else 'warn' if e['percent'] < 100 else 'ok'
            e['missing_labels'] = [S.SECTION_LABEL[m] for m in e['missing']]
            out.append(e)
    return out, avg


@require_GET
@require_role('hr')
def kye(request):
    """HR: Know Your Employee campaigns that keep records current, and the record completeness report."""
    view = 'all' if request.GET.get('view') == 'all' else 'incomplete'
    comp, avg = _completeness_rows(request, view)
    if is_htmx(request) and request.headers.get('HX-Target') == 'kye-completeness':
        return render(request, 'relations/partials/completeness.html', {'completeness': comp, 'view': view, 'avg': avg})
    camps = [_decorate_campaign(c) for c in S.list_campaigns(request.hajiri_user)['campaigns']]
    if is_htmx(request) and request.headers.get('HX-Target') == 'kye-campaigns':
        return render(request, 'relations/partials/campaign_list.html', {'campaigns': camps})
    return render(request, 'relations/kye.html', {'campaigns': camps, 'completeness': comp, 'view': view, 'avg': avg})


@require_http_methods(['GET', 'POST'])
@require_role('hr')
def kye_new(request):
    user = request.hajiri_user
    if request.method == 'GET':
        return render(request, 'relations/partials/kye_form.html', {
            'sections': S.SECTION_LABEL.items(), 'chosen': ['personal', 'contact', 'emergency', 'family', 'bank'],
            'due': (_today() + timedelta(days=14)).isoformat(), 'today': S.today(), **_structure(company_id_of(user))})
    r = S.launch_campaign(user, F.CampaignForm(request.POST).body())
    return _ok(request, f"Sent to {r['employees']} employees.", 'kye-campaigns', close=True)


def _campaign(request, cid, message: str | None = None, *refreshes: str) -> HttpResponse:
    d = S.campaign_detail(request.hajiri_user, str(cid))
    c = d['campaign']
    c['section_labels'] = [S.SECTION_LABEL.get(s, s) for s in c['sections']]
    for r in d['responses']:
        r['status_label'], r['status_tone'] = KYE_RESPONSE.get(r['status'], (r['status'], 'idle'))
    return _render_ok(request, 'relations/partials/campaign_detail.html', {'c': c, 'responses': d['responses']}, message, *refreshes)


@require_GET
@require_role('hr')
def kye_campaign(request, id):
    return _campaign(request, id)


@require_POST
@require_role('hr')
def kye_nudge(request, id):
    r = S.nudge_campaign(request.hajiri_user, str(id))
    return _campaign(request, id, f"Reminded {r['nudged']} people." if r['nudged'] else 'Everyone pending was already reminded today.')


@require_POST
@require_role('hr')
def kye_close(request, id):
    S.close_campaign(request.hajiri_user, str(id))
    return _campaign(request, id, 'Closed.', 'kye-campaigns')


@require_POST
@login_required
def kye_respond(request, id):
    body = F.KyeResponseForm(request.POST).body()
    S.respond_kye(request.hajiri_user, str(id), body)
    if body['status'] == 'changes_requested':
        # "Something needs changing" goes on to the profile, where changes are made (RelationsHomeCard).
        resp = HttpResponse(status=204)
        resp.headers['HX-Redirect'] = url_of('people:profile') or '/me/profile'
        return resp
    return _ok(request, 'Thank you. HR has your confirmation.', 'relations-home')


# ================================================================ registered devices

@require_GET
@require_role('hr')
def devices(request):
    """HR: approve the phones and computers employees may clock in from, and switch the rule on or off."""
    user = request.hajiri_user
    status = 'all' if request.GET.get('status') == 'all' else 'pending'
    lst = S.list_devices(user, {'status': status})['devices']
    for d in lst:
        d['status_label'], d['status_tone'] = DEVICE_HR_STATUS[d['status']]
    ctx = {'devices': lst, 'status': status}
    if is_htmx(request) and request.headers.get('HX-Target') == 'devices':
        return render(request, 'relations/partials/device_list.html', ctx)
    ctx['on'] = S.get_policy(user)['policy']['registeredDevicesOnly']
    return render(request, 'relations/devices.html', ctx)


@require_POST
@require_role('hr')
def device_decide(request, id, action):
    # The service parses the id, then the action (z.enum), so a bad one is a 400 as in TS.
    S.decide_device(request.hajiri_user, str(id), action)
    return _ok(request, {'approve': 'Approved.', 'reject': 'Not approved.', 'revoke': 'Removed.'}[action], 'devices')


@require_GET
@login_required
def my_devices(request):
    """Register this phone or computer for clock-in, when the company requires registered devices."""
    d = S.my_devices(request.hajiri_user)
    for x in d['devices']:
        x['status_label'], x['status_tone'] = DEVICE_MY_STATUS[x['status']]
    ctx = {'devices': d['devices'], 'required': d['required']}
    if is_htmx(request) and request.headers.get('HX-Target') == 'my-devices':
        return render(request, 'relations/partials/my_device_list.html', ctx)
    ua = request.headers.get('User-Agent', '')
    ctx['guess'] = ('iPhone' if 'iPhone' in ua else 'Android phone' if 'Android' in ua else 'Mac' if 'Macintosh' in ua
                    else 'Windows computer' if 'Windows' in ua else 'My device')
    return render(request, 'relations/my_devices.html', ctx)


@require_POST
@login_required
def device_register(request):
    S.register_device(request.hajiri_user, F.DeviceForm(request.POST).body(), request.headers.get('User-Agent'))
    return _ok(request, 'Registered. HR has been asked to approve it.', 'my-devices')


@require_POST
@login_required
def device_remove(request, id):
    S.remove_my_device(request.hajiri_user, str(id))
    return _ok(request, 'Removed.', 'my-devices')


# ================================================================ sign-in pop-ups (Layout.tsx PopupNotices)

def _popup_fragment(request) -> HttpResponse:
    lst = S.popups(request.hajiri_user)['popups'] if request.hajiri_user.company_id else []
    if not lst:
        return HttpResponse('')
    return render(request, 'relations/partials/popup.html', {'p': lst[0]})


@require_GET
@signed_in
def popup_notices(request):
    """One notice at a time, once per person, or until they confirm reading it; an empty 200 when there is none (also
    while the session still has to change its password or set up two-step sign-in: the layout asks on every page)."""
    user = request.hajiri_user
    if user.must_change_password or user.must_set_up_two_factor:
        return HttpResponse('')
    return _popup_fragment(request)


@require_POST
@login_required
def popup_dismiss(request, id):
    try:
        S.dismiss_popup(request.hajiri_user, str(id), F.DismissForm(request.POST).body())
    except HttpError as e:
        r = HttpResponse(status=e.status)
        r.headers['HX-Reswap'] = 'none'
        return trigger(r, 'hj:toast', {'message': error_message(e), 'tone': 'err'})
    return _popup_fragment(request)


# ================================================================ Home card and the employee page's Relations tab

@require_GET
@login_required
def home_card(request):
    """Home: a profile review HR asked for, and any notice from HR waiting for a response. Empty 200 otherwise."""
    user = request.hajiri_user
    kye_ = S.my_kye(user)
    pending = [c for c in kye_['campaigns'] if c['status'] == 'pending']
    notices = [k for k in S.my_cases(user)['cases'] if k['status'] == 'show_cause']
    missing = (kye_.get('completeness') or {}).get('missing') or []
    for c in pending:
        c['chips'] = [{'label': S.SECTION_LABEL.get(s, s) + (': missing' if s in missing else ''), 'missing': s in missing}
                      for s in c['sections']]
    return render(request, 'relations/partials/home_card.html', {'pending': pending, 'notices': notices,
                                                                 'profile_url': url_of('people:profile') or '/me/profile'})


@require_GET
@require_role('hr')
def employee_section(request, employee_id):
    """HR-only tab on the employee page: contract end and retirement, temporary roles, record completeness and
    disciplinary history."""
    user = request.hajiri_user
    eid = str(employee_id)
    contract = S.get_contract(user, eid)
    assigns = [_decorate_assignment(a) for a in S.list_assignments(user, {'employeeId': eid, 'status': 'all'})['assignments']]
    cases = [_decorate_case(k) for k in S.list_cases(user, {'employeeId': eid})['cases']]
    mine = next((e for e in S.completeness(company_id_of(user), eid)), None)
    if mine:
        mine['missing_labels'] = [S.SECTION_LABEL[m] for m in mine['missing']]
    ctx = {'employee_id': eid, 'contract': contract, 'assignments': assigns, 'cases': cases, 'comp': mine}
    target = request.headers.get('HX-Target') if is_htmx(request) else None
    if target == 'relations-assignments':
        return render(request, 'relations/partials/employee_assignments.html', ctx)
    if target == 'relations-cases':
        return render(request, 'relations/partials/employee_cases.html', ctx)
    return render(request, 'relations/partials/employee_section.html', ctx)


@require_POST
@require_role('hr')
def contract_save(request, employee_id):
    S.set_contract(request.hajiri_user, str(employee_id), F.ContractForm(request.POST).body())
    return _ok(request, 'Saved.')
