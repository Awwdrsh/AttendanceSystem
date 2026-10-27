"""
The manager's journal, continuous feedback and 1-on-1s (port of apps/api/src/modules/performance/conversations.ts).
"""
from __future__ import annotations

from datetime import datetime, timedelta

from django.db import transaction

from hajiri.core.auth import AuthUser, company_id_of, has_role, is_manager_of
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Arr, Bool, DateTimeStr, Enum, IsoDate, Obj, Str, Uuid, parse, parse_uuid
from hajiri.notifications.service import notify, user_id_for_employee

from .common import direct_manager, employee_in_company, is_above_in_chain, me

CADENCE_DAYS = {'weekly': 7, 'biweekly': 14, 'monthly': 30}
CADENCES = ('none', 'weekly', 'biweekly', 'monthly')
TAGS = ('positive', 'improvement', 'neutral')
_JOURNAL_ONLY = "only the employee's managers and HR keep a journal"


def _s(r: dict, *keys: str) -> dict:
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


# ---------------------------------------------------------------- manager's journal (private to the author and HR)

def journal(user: AuthUser, query: dict) -> list[dict]:
    q = parse(Obj({'employeeId': Uuid(), 'from': IsoDate(optional=True), 'to': IsoDate(optional=True)}), query)
    company_id = company_id_of(user)
    employee_in_company(company_id, q['employeeId'])
    if not is_above_in_chain(user, q['employeeId']):
        raise ForbiddenError(_JOURNAL_ONLY)
    out = rows("""
        SELECT j.id, j.note_date, j.tag, j.note, j.created_at, j.author_id, u.name AS author_name FROM perf_journal j JOIN app_user u ON u.id = j.author_id
         WHERE j.employee_id = %s AND j.company_id = %s AND (%s OR j.author_id = %s)
           AND (%s::date IS NULL OR j.note_date >= %s) AND (%s::date IS NULL OR j.note_date <= %s)
         ORDER BY j.note_date DESC, j.created_at DESC""",
               [q['employeeId'], company_id, has_role(user, 'hr'), user.id, q.get('from'), q.get('from'), q.get('to'), q.get('to')])
    return [_s(r, 'id', 'author_id') for r in out]


def add_journal(user: AuthUser, body: dict) -> str:
    b = parse(Obj({'employeeId': Uuid(), 'noteDate': IsoDate(), 'tag': Enum(TAGS, default='neutral'),
                   'note': Str(trim=True, min=1, max=4000)}), body)
    company_id = company_id_of(user)
    employee_in_company(company_id, b['employeeId'])
    if not is_above_in_chain(user, b['employeeId']):
        raise ForbiddenError(_JOURNAL_ONLY)
    return str(one('INSERT INTO perf_journal (company_id, employee_id, author_id, note_date, tag, note) VALUES (%s, %s, %s, %s, %s, %s) RETURNING id',
                   [company_id, b['employeeId'], user.id, b['noteDate'], b['tag'], b['note']])['id'])


def delete_journal(user: AuthUser, entry_id: str) -> None:
    if not execute('DELETE FROM perf_journal WHERE id = %s AND company_id = %s AND author_id = %s',
                   [parse_uuid(str(entry_id)), company_id_of(user), user.id]):
        raise NotFoundError('note not found')


# ---------------------------------------------------------------- continuous feedback

FEEDBACK_SELECT = """
  SELECT f.id, f.status, f.prompt, f.body, f.private_to_manager, f.created_at, f.given_at,
         f.giver_employee_id, g.name_en AS giver_name, f.subject_employee_id, s.name_en AS subject_name,
         f.requester_employee_id, rq.name_en AS requester_name
    FROM perf_feedback f JOIN employee g ON g.id = f.giver_employee_id JOIN employee s ON s.id = f.subject_employee_id
    LEFT JOIN employee rq ON rq.id = f.requester_employee_id"""


def feedback(user: AuthUser, query: dict) -> list[dict]:
    """
    received: given feedback about me (not the private-to-manager ones). given: what I wrote. requests: feedback I was
    asked for and requests I made. about: everything about an employee, for their managers and HR.
    """
    q = parse(Obj({'box': Enum(('received', 'given', 'requests', 'about'), default='received'), 'employeeId': Uuid(optional=True)}), query)
    company_id = company_id_of(user)
    if q['box'] == 'about':
        if not q.get('employeeId'):
            raise BadRequestError('employeeId is required')
        employee_in_company(company_id, q['employeeId'])
        if not is_above_in_chain(user, q['employeeId']):
            raise ForbiddenError("only the employee's managers and HR see all feedback")
        out = rows(f"{FEEDBACK_SELECT} WHERE f.company_id = %s AND f.subject_employee_id = %s AND f.status = 'given' ORDER BY f.given_at DESC",
                   [company_id, q['employeeId']])
        return [_s(r, 'id', 'giver_employee_id', 'subject_employee_id', 'requester_employee_id') for r in out]
    emp = me(user)
    where = {
        'received': "f.subject_employee_id = %s AND f.status = 'given' AND NOT f.private_to_manager",
        'given': "f.giver_employee_id = %s AND f.status = 'given'",
        'requests': "((f.giver_employee_id = %s AND f.status = 'requested') OR f.requester_employee_id = %s)",
    }[q['box']]
    params = [company_id, emp] + ([emp] if q['box'] == 'requests' else [])
    out = rows(f'{FEEDBACK_SELECT} WHERE f.company_id = %s AND {where} ORDER BY coalesce(f.given_at, f.created_at) DESC LIMIT 300', params)
    res = []
    for f in out:
        _s(f, 'id', 'giver_employee_id', 'subject_employee_id', 'requester_employee_id')
        # A requester who is the subject sees who answered, but not the text of private-to-manager answers.
        if q['box'] == 'requests' and f['giver_employee_id'] != emp and f['private_to_manager'] and f['subject_employee_id'] == emp:
            f = {**f, 'body': None}
        res.append(f)
    return res


def give_feedback(user: AuthUser, body: dict) -> None:
    b = parse(Obj({'toEmployeeId': Uuid(), 'body': Str(trim=True, min=1, max=4000), 'privateToManager': Bool(default=False)}), body)
    giver = me(user)
    company_id = company_id_of(user)
    if b['toEmployeeId'] == giver:
        raise BadRequestError('you cannot give feedback to yourself')
    employee_in_company(company_id, b['toEmployeeId'])
    with transaction.atomic():
        execute("""INSERT INTO perf_feedback (company_id, giver_employee_id, subject_employee_id, status, body, private_to_manager, given_at)
                   VALUES (%s, %s, %s, 'given', %s, %s, now())""", [company_id, giver, b['toEmployeeId'], b['body'], b['privateToManager']])
        to = direct_manager(b['toEmployeeId']) if b['privateToManager'] else b['toEmployeeId']
        if to:
            notify(company_id=company_id, user_id=user_id_for_employee(to), employee_id=to, template='feedback.given', title='New feedback',
                   body=f'{user.name} shared feedback about a member of your team.' if b['privateToManager'] else f'{user.name} gave you feedback.',
                   payload={})


def request_feedback(user: AuthUser, body: dict) -> None:
    b = parse(Obj({'fromEmployeeIds': Arr(Uuid(), min=1, max=10), 'aboutEmployeeId': Uuid(optional=True),
                   'prompt': Str(trim=True, max=1000, optional=True)}), body)
    requester = me(user)
    company_id = company_id_of(user)
    subject = b.get('aboutEmployeeId') or requester
    employee_in_company(company_id, subject)
    if subject != requester and not is_above_in_chain(user, subject):
        raise ForbiddenError('you can ask for feedback about yourself or your team')
    prompt = b.get('prompt')
    with transaction.atomic():
        for frm in dict.fromkeys(b['fromEmployeeIds']):
            if frm == subject:
                raise BadRequestError('someone cannot give feedback about themselves')
            employee_in_company(company_id, frm)
            execute("""INSERT INTO perf_feedback (company_id, giver_employee_id, subject_employee_id, requester_employee_id, status, prompt)
                       VALUES (%s, %s, %s, %s, 'requested', %s)""", [company_id, frm, subject, requester, prompt])
            notify(company_id=company_id, user_id=user_id_for_employee(frm), employee_id=frm, template='feedback.requested',
                   title='Feedback requested',
                   body=f"{user.name} asked for your feedback{' about a colleague' if subject != requester else ''}"
                        f"{': ' + prompt[:140] if prompt else '.'}", payload={})


def _open_request(company_id: str, fid: str, giver: str) -> dict:
    r = one('SELECT id, status, requester_employee_id, subject_employee_id FROM perf_feedback WHERE id = %s AND company_id = %s AND giver_employee_id = %s',
            [fid, company_id, giver])
    if not r:
        raise NotFoundError('feedback request not found')
    if r['status'] != 'requested':
        raise ConflictError('this request was already answered')
    return _s(r, 'id', 'requester_employee_id', 'subject_employee_id')


def answer_feedback(user: AuthUser, fid: str, body: dict) -> None:
    b = parse(Obj({'body': Str(trim=True, min=1, max=4000), 'privateToManager': Bool(default=False)}), body)
    giver = me(user)
    company_id = company_id_of(user)
    f = _open_request(company_id, parse_uuid(str(fid)), giver)
    with transaction.atomic():
        execute("UPDATE perf_feedback SET status = 'given', body = %s, private_to_manager = %s, given_at = now() WHERE id = %s",
                [b['body'], b['privateToManager'], f['id']])
        if f['requester_employee_id']:
            r = f['requester_employee_id']
            notify(company_id=company_id, user_id=user_id_for_employee(r), employee_id=r, template='feedback.answered', title='Feedback received',
                   body=f'{user.name} answered your feedback request.', payload={})


def decline_feedback(user: AuthUser, fid: str) -> None:
    giver = me(user)
    f = _open_request(company_id_of(user), parse_uuid(str(fid)), giver)
    execute("UPDATE perf_feedback SET status = 'declined' WHERE id = %s", [f['id']])


def get_feedback_request(user: AuthUser, fid: str) -> dict:
    """An open request I was asked to answer (for the answer form)."""
    emp = me(user)
    r = one(f"{FEEDBACK_SELECT} WHERE f.id = %s AND f.company_id = %s AND f.giver_employee_id = %s AND f.status = 'requested'",
            [parse_uuid(str(fid)), company_id_of(user), emp])
    if not r:
        raise NotFoundError('feedback request not found')
    return _s(r, 'id', 'giver_employee_id', 'subject_employee_id', 'requester_employee_id')


# ---------------------------------------------------------------- 1-on-1s (only the two people in the meeting)

def _load_meeting(company_id: str, mid: str, employee_id: str) -> dict:
    m = one('SELECT id, company_id, manager_employee_id, employee_id, scheduled_at, cadence, status FROM perf_one_on_one WHERE id = %s AND company_id = %s',
            [mid, company_id])
    if m:
        _s(m, 'id', 'company_id', 'manager_employee_id', 'employee_id')
    if not m or (m['manager_employee_id'] != employee_id and m['employee_id'] != employee_id):
        raise NotFoundError('1-on-1 not found')
    return {**m, 'side': 'manager' if m['manager_employee_id'] == employee_id else 'employee'}


def meetings(user: AuthUser, with_: str | None = None) -> dict:
    w = parse(Uuid(optional=True), with_) if with_ else None
    emp = me(user)
    company_id = company_id_of(user)
    ms = rows("""
        SELECT o.id, o.manager_employee_id, mg.name_en AS manager_name, o.employee_id, em.name_en AS employee_name, o.scheduled_at, o.cadence, o.status,
               CASE WHEN o.manager_employee_id = %s THEN 'manager' ELSE 'employee' END AS side,
               (SELECT count(*)::int FROM perf_one_on_one_item i WHERE i.meeting_id = o.id AND i.kind = 'action' AND NOT i.done) AS open_actions,
               (SELECT count(*)::int FROM perf_one_on_one_item i WHERE i.meeting_id = o.id AND i.kind = 'agenda') AS agenda_items
          FROM perf_one_on_one o JOIN employee mg ON mg.id = o.manager_employee_id JOIN employee em ON em.id = o.employee_id
         WHERE o.company_id = %s AND (o.manager_employee_id = %s OR o.employee_id = %s)
           AND (%s::uuid IS NULL OR o.manager_employee_id = %s OR o.employee_id = %s)
         ORDER BY o.status = 'scheduled' DESC, CASE WHEN o.status = 'scheduled' THEN o.scheduled_at END ASC, o.scheduled_at DESC LIMIT 200""",
              [emp, company_id, emp, emp, w, w, w])
    # Who I can meet with: my direct reports and my own manager.
    reports = rows("""SELECT e.id, e.name_en, e.designation FROM employee_assignment a JOIN employee e ON e.id = a.employee_id
                       WHERE a.manager_id = %s AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date AND e.status = 'active' ORDER BY e.name_en""", [emp])
    mgr = direct_manager(emp)
    manager = one('SELECT id, name_en, designation FROM employee WHERE id = %s', [mgr]) if mgr else None
    return {'meetings': [_s(m, 'id', 'manager_employee_id', 'employee_id') for m in ms], 'reports': [_s(r, 'id') for r in reports],
            'manager': _s(manager, 'id') if manager else None}


MEETING_BODY = Obj({'withEmployeeId': Uuid(), 'scheduledAt': DateTimeStr(), 'cadence': Enum(CADENCES, default='none'),
                    'agenda': Arr(Str(trim=True, min=1, max=1000), max=20, default=list)})


def create_meeting(user: AuthUser, body: dict) -> str:
    b = parse(MEETING_BODY, body)
    emp = me(user)
    company_id = company_id_of(user)
    employee_in_company(company_id, b['withEmployeeId'])
    if is_manager_of(emp, b['withEmployeeId']):
        manager_id, employee_id = emp, b['withEmployeeId']
    elif is_manager_of(b['withEmployeeId'], emp):
        manager_id, employee_id = b['withEmployeeId'], emp
    else:
        raise ForbiddenError('1-on-1s are between a person and their direct manager')
    with transaction.atomic():
        mid = str(one("""INSERT INTO perf_one_on_one (company_id, manager_employee_id, employee_id, scheduled_at, cadence, created_by)
                         VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                      [company_id, manager_id, employee_id, b['scheduledAt'], b['cadence'], user.id])['id'])
        for t in b['agenda']:
            execute("INSERT INTO perf_one_on_one_item (meeting_id, kind, text, added_by) VALUES (%s, 'agenda', %s, %s)", [mid, t, user.id])
        other = b['withEmployeeId']
        notify(company_id=company_id, user_id=user_id_for_employee(other), employee_id=other, template='one_on_one.scheduled',
               title='1-on-1 scheduled', body=f'{user.name} scheduled a 1-on-1 with you.', payload={'meetingId': mid})
    return mid


def meeting_detail(user: AuthUser, mid: str) -> dict:
    emp = me(user)
    company_id = company_id_of(user)
    m = _load_meeting(company_id, parse_uuid(str(mid)), emp)
    row = one("""SELECT o.*, mg.name_en AS manager_name, em.name_en AS employee_name FROM perf_one_on_one o
                   JOIN employee mg ON mg.id = o.manager_employee_id JOIN employee em ON em.id = o.employee_id WHERE o.id = %s""", [m['id']])
    mpn = row.pop('manager_private_notes')
    epn = row.pop('employee_private_notes')
    _s(row, 'id', 'company_id', 'manager_employee_id', 'employee_id', 'created_by')
    items = rows("""SELECT i.id, i.kind, i.text, i.owner_employee_id, o.name_en AS owner_name, i.done, i.created_at, i.added_by FROM perf_one_on_one_item i
                     LEFT JOIN employee o ON o.id = i.owner_employee_id WHERE i.meeting_id = %s ORDER BY i.kind, i.created_at""", [m['id']])
    # Earlier open action items between the same two people, so nothing agreed is forgotten.
    carried = rows("""SELECT i.id, i.text, i.owner_employee_id, o.name_en AS owner_name, p.scheduled_at FROM perf_one_on_one_item i
                        JOIN perf_one_on_one p ON p.id = i.meeting_id LEFT JOIN employee o ON o.id = i.owner_employee_id
                       WHERE p.manager_employee_id = %s AND p.employee_id = %s AND p.id <> %s AND i.kind = 'action' AND NOT i.done
                       ORDER BY p.scheduled_at""", [m['manager_employee_id'], m['employee_id'], m['id']])
    return {'meeting': {**row, 'side': m['side'], 'private_notes': mpn if m['side'] == 'manager' else epn},
            'items': [_s(i, 'id', 'owner_employee_id', 'added_by') for i in items],
            'openActions': [_s(i, 'id', 'owner_employee_id') for i in carried]}


MEETING_PATCH = Obj({'scheduledAt': DateTimeStr(optional=True), 'cadence': Enum(CADENCES, optional=True),
                     'sharedNotes': Str(max=8000, nullish=True), 'privateNotes': Str(max=8000, nullish=True),
                     'status': Enum(('scheduled', 'done', 'cancelled'), optional=True)})


def update_meeting(user: AuthUser, mid: str, body: dict) -> str | None:
    """Returns the id of the next meeting when a recurring one is marked done."""
    b = parse(MEETING_PATCH, body)
    emp = me(user)
    company_id = company_id_of(user)
    with transaction.atomic():
        m = _load_meeting(company_id, parse_uuid(str(mid)), emp)
        sets, vals = [], []

        def put(col, v):
            sets.append(f'{col} = %s')
            vals.append(v)
        if 'scheduledAt' in b:
            put('scheduled_at', b['scheduledAt'])
        if 'cadence' in b:
            put('cadence', b['cadence'])
        if 'sharedNotes' in b:
            put('shared_notes', b['sharedNotes'])
        if 'privateNotes' in b:
            put('manager_private_notes' if m['side'] == 'manager' else 'employee_private_notes', b['privateNotes'])
        if 'status' in b:
            put('status', b['status'])
        if sets:
            execute(f"UPDATE perf_one_on_one SET {', '.join(sets)} WHERE id = %s", [*vals, m['id']])
        next_id = None
        cadence = b.get('cadence') or m['cadence']
        if b.get('status') == 'done' and m['status'] != 'done' and CADENCE_DAYS.get(cadence):
            base = datetime.fromisoformat(b['scheduledAt'].replace('Z', '+00:00')) if 'scheduledAt' in b else m['scheduled_at']
            nxt = base + timedelta(days=CADENCE_DAYS[cadence])
            next_id = str(one("""INSERT INTO perf_one_on_one (company_id, manager_employee_id, employee_id, scheduled_at, cadence, created_by)
                                 VALUES (%s, %s, %s, %s, %s, %s) RETURNING id""",
                              [company_id, m['manager_employee_id'], m['employee_id'], nxt, cadence, user.id])['id'])
    return next_id


def add_meeting_item(user: AuthUser, mid: str, body: dict) -> str:
    b = parse(Obj({'kind': Enum(('agenda', 'action')), 'text': Str(trim=True, min=1, max=1000), 'ownerEmployeeId': Uuid(nullish=True)}), body)
    emp = me(user)
    m = _load_meeting(company_id_of(user), parse_uuid(str(mid)), emp)
    owner = b.get('ownerEmployeeId')
    if owner and owner not in (m['manager_employee_id'], m['employee_id']):
        raise BadRequestError('the owner must be one of the two people in the meeting')
    return str(one('INSERT INTO perf_one_on_one_item (meeting_id, kind, text, owner_employee_id, added_by) VALUES (%s, %s, %s, %s, %s) RETURNING id',
                   [m['id'], b['kind'], b['text'], owner, user.id])['id'])


def _item_meeting(company_id: str, item_id: str, emp: str) -> dict:
    r = one('SELECT meeting_id FROM perf_one_on_one_item WHERE id = %s', [item_id])
    if not r:
        raise NotFoundError('item not found')
    return _load_meeting(company_id, str(r['meeting_id']), emp)


def update_meeting_item(user: AuthUser, item_id: str, body: dict) -> dict:
    b = parse(Obj({'done': Bool(optional=True), 'text': Str(trim=True, min=1, max=1000, optional=True)}), body)
    item_id = parse_uuid(str(item_id))
    m = _item_meeting(company_id_of(user), item_id, me(user))
    execute('UPDATE perf_one_on_one_item SET done = coalesce(%s, done), text = coalesce(%s, text) WHERE id = %s',
            [b.get('done'), b.get('text'), item_id])
    return m


def delete_meeting_item(user: AuthUser, item_id: str) -> dict:
    item_id = parse_uuid(str(item_id))
    m = _item_meeting(company_id_of(user), item_id, me(user))
    execute('DELETE FROM perf_one_on_one_item WHERE id = %s', [item_id])
    return m
