"""
Learning: course catalogue, sessions, assessments, enrolments (self-nomination through approvals, assignment,
mandatory training), certificates, expiry reminders and spend (port of apps/api/src/modules/learning/service.ts and
routes.ts). Bodies use the TS camelCase shape.
"""
from __future__ import annotations

import calendar
import json
from datetime import date

from django.db import IntegrityError, transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import (Arr, Bool, DateTimeStr, Enum, Int, IsoDate, Money, Num, Obj, S, Str, Uuid, parse, parse_uuid,
                                _fail)
from hajiri.notifications.service import notify, notify_role, user_id_for_employee
from hajiri.performance.common import SUBORDINATES_CTE, employee_in_company, esc, first_time, fmt_number, is_above_in_chain, num
from hajiri.workflow.service import DecisionContext, cancel_workflow, npt_today, register_handler, start_workflow

MAX_ATTEMPTS = 5
ENROLL_STATUSES = ('pending_approval', 'enrolled', 'completed', 'rejected', 'cancelled', 'no_show')


def add_months(iso: str, months: int) -> str:
    """'YYYY-MM-DD' plus whole months, clamped to the end of a shorter month (31 Jan + 1 month = 28/29 Feb)."""
    y, m, d = (int(x) for x in str(iso)[:10].split('-'))
    total = (m - 1) + months
    ty, tm = y + total // 12, total % 12 + 1
    last = calendar.monthrange(ty, tm)[1]
    return date(ty, tm, min(d, last)).isoformat()


def _status_text(s: str) -> str:
    return s.replace('_', ' ', 1)


def complete_enrollment(enrollment_id: str, attended: bool | None = None) -> None:
    """Mark an enrolment complete and issue the certificate dates from the course's validity."""
    e = one("""SELECT e.status, c.validity_months, e.company_id, e.employee_id, c.title FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id
                WHERE e.id = %s FOR UPDATE OF e""", [enrollment_id])
    if not e:
        raise NotFoundError('enrolment not found')
    if e['status'] == 'completed':
        return
    if e['status'] != 'enrolled':
        raise ConflictError(f"this enrolment is {_status_text(e['status'])}")
    today = npt_today()
    execute("""UPDATE lrn_enrollment SET status = 'completed', completed_at = now(), attended = coalesce(%s, attended),
                      certificate_issued_on = %s, certificate_expires_on = %s, expiry_reminded_at = NULL WHERE id = %s""",
            [attended, today, add_months(today, e['validity_months']) if e['validity_months'] else None, enrollment_id])
    emp = str(e['employee_id'])
    notify(company_id=str(e['company_id']), user_id=user_id_for_employee(emp), employee_id=emp, template='learning.completed',
           title='Training completed', body=f"You completed {e['title']}. Your certificate is under Growth, Learning.",
           payload={'enrollmentId': str(enrollment_id)})


def create_review_nominations(company_id: str, employee_id: str, review_id: str, course_ids: list[str], actor_id: str) -> int:
    """Training needs from a released review become enrolments the employee can see."""
    n = 0
    for course_id in course_ids:
        c = one('SELECT cost, title FROM lrn_course WHERE id = %s AND company_id = %s AND active', [course_id, company_id])
        if not c:
            continue
        if execute("""INSERT INTO lrn_enrollment (company_id, course_id, employee_id, source, status, review_id, assigned_by, cost)
                      VALUES (%s, %s, %s, 'review', 'enrolled', %s, %s, %s) ON CONFLICT DO NOTHING""",
                   [company_id, course_id, employee_id, review_id, actor_id, c['cost']]):
            n += 1
            notify(company_id=company_id, user_id=user_id_for_employee(employee_id), employee_id=employee_id, template='learning.assigned',
                   title='Training assigned', body=f"From your review: {c['title']}.", payload={'courseId': str(course_id)})
    return n


def apply_nomination_decision(enrollment_id: str, state: str, ctx: DecisionContext) -> None:
    e = one("""SELECT e.status, e.company_id, e.employee_id, c.title FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id
                WHERE e.id = %s FOR UPDATE OF e""", [enrollment_id])
    if not e:
        raise NotFoundError('enrolment not found')
    if e['status'] != 'pending_approval':
        raise ConflictError(f"nomination already {e['status']}")
    execute('UPDATE lrn_enrollment SET status = %s WHERE id = %s', ['enrolled' if state == 'approved' else 'rejected', enrollment_id])
    emp = str(e['employee_id'])
    notify(company_id=str(e['company_id']), user_id=user_id_for_employee(emp), employee_id=emp, template=f'learning.nomination_{state}',
           title='Training approved' if state == 'approved' else 'Training not approved',
           body=f"{e['title']}{f' ({ctx.reason})' if ctx.reason else ''}.", payload={'enrollmentId': str(enrollment_id)})


register_handler('training_nomination', apply_nomination_decision)


def learning_reminders(now=None) -> dict:
    """
    Daily: certificates of mandatory or expiring courses that run out within 30 days (employee and HR are told once per
    certificate), and reminders the day before a scheduled session.
    """
    today = npt_today()
    exp = rows("""
        SELECT e.id, e.company_id, e.employee_id, c.title, e.certificate_expires_on, em.name_en
          FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id JOIN employee em ON em.id = e.employee_id
         WHERE e.status = 'completed' AND e.certificate_expires_on IS NOT NULL AND e.expiry_reminded_at IS NULL AND em.status = 'active'
           AND e.certificate_expires_on <= %s::date + 30
           AND NOT EXISTS (SELECT 1 FROM lrn_enrollment n WHERE n.course_id = e.course_id AND n.employee_id = e.employee_id AND n.created_at > e.created_at)""",
               [today])
    for e in exp:
        past = e['certificate_expires_on'].isoformat() < today
        body = f"{e['title']} certificate {'expired' if past else 'expires'} on {bs_label(e['certificate_expires_on'])}."
        title = 'Certificate expired' if past else 'Certificate expiring'
        emp, co = str(e['employee_id']), str(e['company_id'])
        notify(company_id=co, user_id=user_id_for_employee(emp), employee_id=emp, template='learning.certificate_expiring', title=title,
               body=f'{body} Ask HR for a refresher session.', payload={'enrollmentId': str(e['id'])})
        notify_role(co, 'hr', employee_id=emp, template='learning.certificate_expiring', title=title, body=f"{e['name_en']}: {body}",
                    payload={'enrollmentId': str(e['id'])})
        execute('UPDATE lrn_enrollment SET expiry_reminded_at = now() WHERE id = %s', [e['id']])
    sess = rows("""
        SELECT e.id, e.company_id, e.employee_id, c.title, s.starts_at, s.venue FROM lrn_enrollment e JOIN lrn_session s ON s.id = e.session_id
          JOIN lrn_course c ON c.id = e.course_id
         WHERE e.status = 'enrolled' AND s.status = 'scheduled' AND (s.starts_at AT TIME ZONE 'Asia/Kathmandu')::date = %s::date + 1""", [today])
    sessions = 0
    for e in sess:
        if not first_time(f"session:{e['id']}:{today}"):
            continue
        sessions += 1
        emp = str(e['employee_id'])
        notify(company_id=str(e['company_id']), user_id=user_id_for_employee(emp), employee_id=emp, template='learning.session_tomorrow',
               title='Training tomorrow', body=f"{e['title']}{' at ' + e['venue'] if e['venue'] else ''}.", payload={'enrollmentId': str(e['id'])})
    return {'expiring': len(exp), 'sessions': sessions}


# ---------------------------------------------------------------- schemas

class HttpUrl(S):
    """z.url({protocol: /^https?$/}).max(500).nullish().or(z.literal('') -> null)."""

    def check(self, v, path):
        if v == '':
            return None
        import re
        if not isinstance(v, str) or not re.fullmatch(r'https?://[^\s/$.?#][^\s]*', v):
            _fail(path, 'Invalid URL')
        if len(v) > 500:
            _fail(path, 'Too big: expected string to have <=500 characters')
        return v


def _course_fields(partial: bool = False) -> dict:
    o = {'optional': True} if partial else {}
    d = lambda v: o or {'default': v}  # noqa: E731
    return {
        'code': Str(trim=True, min=1, max=30, **o),
        'title': Str(trim=True, min=1, max=200, **o),
        'description': Str(trim=True, max=4000, nullish=True),
        'category': Str(trim=True, max=60, nullish=True),
        'delivery': Enum(('online', 'classroom', 'blended'), **d('classroom')),
        'source': Enum(('internal', 'external'), **d('internal')),
        'provider': Str(trim=True, max=120, nullish=True),
        'durationHours': Num(min=0, max=10000, coerce=True, nullish=True),
        'url': HttpUrl(nullish=True),
        'cost': Money(**d('0')),
        'mandatory': Bool(**d(False)),
        'mandatoryFor': Arr(Str(trim=True, min=1, max=120), max=100, **d(list)),
        'validityMonths': Int(min=1, max=120, coerce=True, nullish=True),
        'passMark': Int(min=1, max=100, coerce=True, nullish=True),
        'active': Bool(**d(True)),
    }


COURSE_BODY = Obj(_course_fields())
COURSE_PATCH = Obj(_course_fields(partial=True))  # only what is sent changes


def _session_fields(partial: bool = False) -> dict:
    o = {'optional': True} if partial else {}
    return {
        'startsAt': DateTimeStr(**o), 'endsAt': DateTimeStr(**o),
        'venue': Str(trim=True, max=200, nullish=True), 'trainer': Str(trim=True, max=120, nullish=True),
        'capacity': Int(min=1, max=10000, coerce=True, nullish=True),
        'status': Enum(('scheduled', 'completed', 'cancelled'), **(o or {'default': 'scheduled'})),
    }


SESSION_BODY = Obj(_session_fields())
SESSION_PATCH = Obj(_session_fields(partial=True))
QUESTION = Obj({'prompt': Str(trim=True, min=1, max=1000), 'options': Arr(Str(trim=True, min=1, max=300), min=2, max=6),
                'correctIndex': Int(min=0)},
               refine=(lambda q: q['correctIndex'] < len(q['options']), 'the correct answer must be one of the options'))


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def _s(r: dict | None, *keys) -> dict | None:
    if r:
        for k in keys:
            if r.get(k) is not None:
                r[k] = str(r[k])
    return r


def _ts(iso: str):
    from datetime import datetime
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


# ---------------------------------------------------------------- catalogue

def list_courses(user: AuthUser) -> list[dict]:
    out = rows("""
        SELECT c.*, (SELECT count(*)::int FROM lrn_question q WHERE q.course_id = c.id) AS questions,
               (SELECT count(*)::int FROM lrn_enrollment e WHERE e.course_id = c.id AND e.status = 'completed') AS completed,
               (SELECT count(*)::int FROM lrn_enrollment e WHERE e.course_id = c.id AND e.status IN ('enrolled', 'pending_approval')) AS enrolled,
               (SELECT min(s.starts_at) FROM lrn_session s WHERE s.course_id = c.id AND s.status = 'scheduled' AND s.starts_at > now()) AS next_session
          FROM lrn_course c WHERE c.company_id = %s AND (c.active OR %s) ORDER BY c.mandatory DESC, c.title""",
               [company_id_of(user), has_role(user, 'hr')])
    return [_s(c, 'id', 'company_id') for c in out]


def course_detail(user: AuthUser, course_id: str) -> dict:
    company_id = company_id_of(user)
    hr = has_role(user, 'hr')
    c = one('SELECT * FROM lrn_course WHERE id = %s AND company_id = %s AND (active OR %s)', [parse_uuid(str(course_id)), company_id, hr])
    if not c:
        raise NotFoundError('course not found')
    _s(c, 'id', 'company_id')
    sessions = rows("""SELECT s.*, (SELECT count(*)::int FROM lrn_enrollment e WHERE e.session_id = s.id
                                     AND e.status IN ('pending_approval', 'enrolled', 'completed')) AS taken
                         FROM lrn_session s WHERE s.course_id = %s ORDER BY s.starts_at DESC""", [c['id']])
    # Answers stay on the server; HR sees them to edit the assessment.
    qs = rows(f"SELECT id, prompt, options{', correct_index' if hr else ''} FROM lrn_question WHERE course_id = %s ORDER BY sort, id", [c['id']])
    return {'course': c, 'sessions': [_s(s, 'id', 'course_id', 'company_id') for s in sessions],
            'questions': [_s(q, 'id') for q in qs] if hr else [], 'questionCount': len(qs)}


def create_course(user: AuthUser, body: dict) -> str:
    _require_hr(user)
    b = parse(COURSE_BODY, body)
    return str(one("""
        INSERT INTO lrn_course (company_id, code, title, description, category, delivery, source, provider, duration_hours, url, cost, mandatory,
                                mandatory_for, validity_months, pass_mark, active)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        [company_id_of(user), b['code'], b['title'], b.get('description'), b.get('category'), b['delivery'], b['source'], b.get('provider'),
         b.get('durationHours'), b.get('url'), b['cost'], b['mandatory'], b['mandatoryFor'], b.get('validityMonths'), b.get('passMark'),
         b['active']])['id'])


def _apply(table: str, row_id: str, company_id: str, b: dict, mapping: dict, missing: str) -> None:
    sets, vals = [], []
    for k, col in mapping.items():
        if k in b:
            sets.append(f'{col} = %s')
            vals.append(b[k])
    if not sets:
        return
    if not execute(f"UPDATE {table} SET {', '.join(sets)} WHERE id = %s AND company_id = %s", [*vals, row_id, company_id]):
        raise NotFoundError(missing)


def update_course(user: AuthUser, course_id: str, body: dict) -> None:
    _require_hr(user)
    b = parse(COURSE_PATCH, body)
    _apply('lrn_course', parse_uuid(str(course_id)), company_id_of(user), b, {
        'code': 'code', 'title': 'title', 'description': 'description', 'category': 'category', 'delivery': 'delivery', 'source': 'source',
        'provider': 'provider', 'durationHours': 'duration_hours', 'url': 'url', 'cost': 'cost', 'mandatory': 'mandatory',
        'mandatoryFor': 'mandatory_for', 'validityMonths': 'validity_months', 'passMark': 'pass_mark', 'active': 'active'}, 'course not found')


def save_questions(user: AuthUser, course_id: str, body: dict) -> None:
    """Replace the course's multiple-choice assessment."""
    _require_hr(user)
    b = parse(Obj({'questions': Arr(QUESTION, max=50)}), body)
    cid = parse_uuid(str(course_id))
    with transaction.atomic():
        if not one('SELECT 1 AS x FROM lrn_course WHERE id = %s AND company_id = %s', [cid, company_id_of(user)]):
            raise NotFoundError('course not found')
        execute('DELETE FROM lrn_question WHERE course_id = %s', [cid])
        for sort, q in enumerate(b['questions']):
            execute('INSERT INTO lrn_question (course_id, prompt, options, correct_index, sort) VALUES (%s, %s, %s, %s, %s)',
                    [cid, q['prompt'], json.dumps(q['options']), q['correctIndex'], sort])
        if b['questions']:
            execute('UPDATE lrn_course SET pass_mark = coalesce(pass_mark, 60) WHERE id = %s', [cid])


def add_session(user: AuthUser, course_id: str, body: dict) -> str:
    _require_hr(user)
    b = parse(SESSION_BODY, body)
    if _ts(b['endsAt']) <= _ts(b['startsAt']):
        raise BadRequestError('the session ends before it starts')
    cid = parse_uuid(str(course_id))
    company_id = company_id_of(user)
    if not one('SELECT 1 AS x FROM lrn_course WHERE id = %s AND company_id = %s', [cid, company_id]):
        raise NotFoundError('course not found')
    return str(one("""INSERT INTO lrn_session (company_id, course_id, starts_at, ends_at, venue, trainer, capacity, status)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                   [company_id, cid, b['startsAt'], b['endsAt'], b.get('venue'), b.get('trainer'), b.get('capacity'), b['status']])['id'])


def update_session(user: AuthUser, session_id: str, body: dict) -> None:
    _require_hr(user)
    b = parse(SESSION_PATCH, body)
    _apply('lrn_session', parse_uuid(str(session_id)), company_id_of(user), b, {
        'startsAt': 'starts_at', 'endsAt': 'ends_at', 'venue': 'venue', 'trainer': 'trainer', 'capacity': 'capacity', 'status': 'status'},
        'session not found')


# ---------------------------------------------------------------- enrolments

ENROLLMENT_SELECT = """
  SELECT e.id, e.course_id, c.code, c.title, c.delivery, c.mandatory, c.pass_mark, c.validity_months, e.session_id, s.starts_at, s.ends_at, s.venue,
         e.employee_id, em.name_en, em.emp_code, e.source, e.status, e.attended, e.completed_at, e.score, e.passed, e.feedback_rating, e.feedback_comment,
         e.certificate_file_id, e.certificate_issued_on, e.certificate_expires_on, e.cost, e.reason, e.created_at,
         (SELECT count(*)::int FROM lrn_question q WHERE q.course_id = c.id) AS questions,
         (SELECT count(*)::int FROM lrn_attempt a WHERE a.enrollment_id = e.id) AS attempts
    FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id JOIN employee em ON em.id = e.employee_id
    LEFT JOIN lrn_session s ON s.id = e.session_id"""
_EID = ('id', 'course_id', 'session_id', 'employee_id', 'certificate_file_id')


def load_enrollment(company_id: str, eid: str) -> dict:
    r = one("""SELECT e.id, e.company_id, e.course_id, e.session_id, e.employee_id, e.status, e.workflow_instance_id, e.certificate_file_id, c.delivery,
                      c.pass_mark, c.title, (SELECT count(*)::int FROM lrn_question q WHERE q.course_id = c.id) AS questions
                 FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id WHERE e.id = %s AND e.company_id = %s""", [eid, company_id])
    if not r:
        raise NotFoundError('enrolment not found')
    return _s(r, 'id', 'company_id', 'course_id', 'session_id', 'employee_id', 'workflow_instance_id', 'certificate_file_id')


def enrollment_role(user: AuthUser, e: dict) -> str | None:
    """The learner, their managers and HR may see an enrolment; only managers and HR may manage it."""
    if user.employee_id == e['employee_id']:
        return 'self'
    if is_above_in_chain(user, e['employee_id']):
        return 'manager'
    return None


def _session_has_room(session_id: str, course_id: str, company_id: str) -> None:
    s = one("""SELECT s.capacity, s.status, (SELECT count(*)::int FROM lrn_enrollment e WHERE e.session_id = s.id
                                               AND e.status IN ('pending_approval', 'enrolled', 'completed')) AS taken
                 FROM lrn_session s WHERE s.id = %s AND s.course_id = %s AND s.company_id = %s""", [session_id, course_id, company_id])
    if not s:
        raise NotFoundError('session not found')
    if s['status'] != 'scheduled':
        raise ConflictError('this session is not open')
    if s['capacity'] is not None and s['taken'] >= s['capacity']:
        raise ConflictError('this session is full')


def list_enrollments(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = parse(Obj({'scope': Enum(('mine', 'team', 'all'), default='mine'), 'courseId': Uuid(optional=True), 'status': Str(max=30, optional=True)}),
              query or {})
    company_id = company_id_of(user)
    filters = 'e.company_id = %s AND (%s::uuid IS NULL OR e.course_id = %s) AND (%s::text IS NULL OR e.status = %s)'
    fp = [company_id, q.get('courseId'), q.get('courseId'), q.get('status'), q.get('status')]
    if q['scope'] == 'mine':
        if not user.employee_id:
            return []
        out = rows(f'{ENROLLMENT_SELECT} WHERE {filters} AND e.employee_id = %s ORDER BY e.created_at DESC', [*fp, user.employee_id])
    elif q['scope'] == 'all' or not user.employee_id:
        if not has_role(user, 'hr'):
            raise ForbiddenError('requires HR')
        out = rows(f'{ENROLLMENT_SELECT} WHERE {filters} ORDER BY e.created_at DESC LIMIT 1000', fp)
    else:
        out = rows(f'{SUBORDINATES_CTE} {ENROLLMENT_SELECT} WHERE {filters} AND e.employee_id IN (SELECT emp FROM subs) ORDER BY e.created_at DESC LIMIT 1000',
                   [user.employee_id, *fp])
    return [_s(r, *_EID) for r in out]


def self_enroll(user: AuthUser, body: dict) -> str:
    """Ask to join a course; the manager approves through the approvals inbox."""
    b = parse(Obj({'courseId': Uuid(), 'sessionId': Uuid(optional=True), 'reason': Str(trim=True, max=1000, optional=True)}), body)
    company_id = company_id_of(user)
    if not user.employee_id:
        raise ForbiddenError('this account is not linked to an employee')
    c = one('SELECT title, cost FROM lrn_course WHERE id = %s AND company_id = %s AND active', [b['courseId'], company_id])
    if not c:
        raise NotFoundError('course not found')
    if b.get('sessionId'):
        _session_has_room(b['sessionId'], b['courseId'], company_id)
    with transaction.atomic():
        try:
            with transaction.atomic():
                eid = str(one("""INSERT INTO lrn_enrollment (company_id, course_id, session_id, employee_id, source, status, reason, cost)
                                 VALUES (%s, %s, %s, %s, 'self', 'pending_approval', %s, %s) RETURNING id""",
                              [company_id, b['courseId'], b.get('sessionId'), user.employee_id, b.get('reason'), c['cost']])['id'])
        except IntegrityError as e:
            if getattr(getattr(e, '__cause__', None), 'sqlstate', None) == '23505':
                raise ConflictError('you are already enrolled or waiting for approval for this course')
            raise
        reason = b.get('reason')
        inst = start_workflow(company_id=company_id, object_type='training_nomination', object_id=eid, employee_id=user.employee_id,
                              title='Training request', body=f"{user.name} asked to join {c['title']}{': ' + reason if reason else ''}")
        execute('UPDATE lrn_enrollment SET workflow_instance_id = %s WHERE id = %s', [inst['id'], eid])
    return eid


def assign(user: AuthUser, body: dict) -> dict:
    """HR assigns anyone; a manager assigns their team. Assigned enrolments need no approval."""
    b = parse(Obj({'courseId': Uuid(), 'sessionId': Uuid(optional=True), 'employeeIds': Arr(Uuid(), min=1, max=500)}), body)
    company_id = company_id_of(user)
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('only managers and HR assign training')
    c = one('SELECT title, cost FROM lrn_course WHERE id = %s AND company_id = %s AND active', [b['courseId'], company_id])
    if not c:
        raise NotFoundError('course not found')
    added = skipped = 0
    with transaction.atomic():
        for emp in dict.fromkeys(b['employeeIds']):
            employee_in_company(company_id, emp)
            if not is_above_in_chain(user, emp):
                raise ForbiddenError('you can assign training only to your team')
            if b.get('sessionId'):
                _session_has_room(b['sessionId'], b['courseId'], company_id)
            if execute("""INSERT INTO lrn_enrollment (company_id, course_id, session_id, employee_id, source, status, assigned_by, cost)
                          VALUES (%s, %s, %s, %s, 'assigned', 'enrolled', %s, %s) ON CONFLICT DO NOTHING""",
                       [company_id, b['courseId'], b.get('sessionId'), emp, user.id, c['cost']]):
                added += 1
                notify(company_id=company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='learning.assigned',
                       title='Training assigned', body=f"{user.name} enrolled you in {c['title']}.", payload={'courseId': b['courseId']})
            else:
                skipped += 1
    return {'added': added, 'skipped': skipped}


def assign_mandatory(user: AuthUser, course_id: str) -> int:
    """Enrol everyone a mandatory course applies to who has no open enrolment and no certificate still valid."""
    _require_hr(user)
    cid = parse_uuid(str(course_id))
    company_id = company_id_of(user)
    course = one('SELECT title, cost, mandatory, mandatory_for FROM lrn_course WHERE id = %s AND company_id = %s AND active', [cid, company_id])
    if not course:
        raise NotFoundError('course not found')
    if not course['mandatory']:
        raise BadRequestError('mark the course mandatory first')
    with transaction.atomic():
        emps = rows("""
            SELECT e.id FROM employee e WHERE e.company_id = %s AND e.status = 'active' AND (cardinality(%s::text[]) = 0 OR e.designation = ANY(%s::text[]))
               AND NOT EXISTS (SELECT 1 FROM lrn_enrollment x WHERE x.employee_id = e.id AND x.course_id = %s
                                AND (x.status IN ('pending_approval', 'enrolled') OR (x.status = 'completed'
                                     AND (x.certificate_expires_on IS NULL OR x.certificate_expires_on > (now() AT TIME ZONE 'Asia/Kathmandu')::date + 30))))""",
                    [company_id, course['mandatory_for'], course['mandatory_for'], cid])
        for e in emps:
            emp = str(e['id'])
            execute("""INSERT INTO lrn_enrollment (company_id, course_id, employee_id, source, status, assigned_by, cost)
                       VALUES (%s, %s, %s, 'mandatory', 'enrolled', %s, %s) ON CONFLICT DO NOTHING""", [company_id, cid, emp, user.id, course['cost']])
            notify(company_id=company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='learning.assigned', title='Mandatory training',
                   body=f"You are enrolled in {course['title']}.", payload={'courseId': cid})
    return len(emps)


def cancel(user: AuthUser, eid: str) -> None:
    company_id = company_id_of(user)
    e = load_enrollment(company_id, parse_uuid(str(eid)))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    if e['status'] not in ('pending_approval', 'enrolled'):
        raise ConflictError(f"this enrolment is {_status_text(e['status'])}")
    with transaction.atomic():
        execute("UPDATE lrn_enrollment SET status = 'cancelled' WHERE id = %s", [e['id']])
        if e['status'] == 'pending_approval':
            cancel_workflow('training_nomination', e['id'])


def record_attendance(user: AuthUser, eid: str, body: dict) -> None:
    """Trainer's register: attended completes the course (unless an assessment is still to pass); absent is a no-show."""
    b = parse(Obj({'attended': Bool()}), body)
    e = load_enrollment(company_id_of(user), parse_uuid(str(eid)))
    role = enrollment_role(user, e)
    if not role:
        raise NotFoundError('enrolment not found')
    if role != 'manager':
        raise ForbiddenError('managers and HR record attendance')
    if e['status'] != 'enrolled':
        raise ConflictError(f"this enrolment is {_status_text(e['status'])}")
    with transaction.atomic():
        if not b['attended']:
            execute("UPDATE lrn_enrollment SET attended = false, status = 'no_show' WHERE id = %s", [e['id']])
        elif e['questions'] > 0:
            execute('UPDATE lrn_enrollment SET attended = true WHERE id = %s', [e['id']])
        else:
            complete_enrollment(e['id'], attended=True)
        audit('attendance', 'lrn_enrollment', actor_id=user.id, row_id=e['id'], after=b)


def complete(user: AuthUser, eid: str) -> None:
    """Online courses without an assessment are completed by the learner; managers and HR may complete any."""
    e = load_enrollment(company_id_of(user), parse_uuid(str(eid)))
    role = enrollment_role(user, e)
    if not role:
        raise NotFoundError('enrolment not found')
    if role == 'self':
        if e['questions'] > 0:
            raise ConflictError('pass the assessment to complete this course')
        if e['delivery'] != 'online':
            raise ForbiddenError('classroom attendance is recorded by the trainer or HR')
    with transaction.atomic():
        complete_enrollment(e['id'])


def assessment(user: AuthUser, eid: str) -> dict:
    e = load_enrollment(company_id_of(user), parse_uuid(str(eid)))
    if user.employee_id != e['employee_id']:
        raise NotFoundError('enrolment not found')
    qs = rows('SELECT id, prompt, options FROM lrn_question WHERE course_id = %s ORDER BY sort, id', [e['course_id']])
    attempts = rows('SELECT score, passed, created_at FROM lrn_attempt WHERE enrollment_id = %s ORDER BY created_at DESC', [e['id']])
    return {'title': e['title'], 'passMark': e['pass_mark'], 'questions': [_s(q, 'id') for q in qs], 'attempts': attempts,
            'attemptsLeft': max(0, MAX_ATTEMPTS - len(attempts))}


def attempt(user: AuthUser, eid: str, body: dict) -> dict:
    b = parse(Obj({'answers': Arr(Int(min=0, max=10), max=50)}), body)
    company_id = company_id_of(user)
    with transaction.atomic():
        e = load_enrollment(company_id, parse_uuid(str(eid)))
        if user.employee_id != e['employee_id']:
            raise NotFoundError('enrolment not found')
        if e['status'] != 'enrolled':
            raise ConflictError('you already completed this course' if e['status'] == 'completed' else f"this enrolment is {_status_text(e['status'])}")
        one('SELECT 1 AS x FROM lrn_enrollment WHERE id = %s FOR UPDATE', [e['id']])
        qs = rows('SELECT correct_index FROM lrn_question WHERE course_id = %s ORDER BY sort, id', [e['course_id']])
        if not qs:
            raise BadRequestError('this course has no assessment')
        if len(b['answers']) != len(qs):
            raise BadRequestError(f'answer all {len(qs)} questions')
        used = one('SELECT count(*)::int AS n FROM lrn_attempt WHERE enrollment_id = %s', [e['id']])['n']
        if used >= MAX_ATTEMPTS:
            raise ConflictError(f'no attempts left (maximum {MAX_ATTEMPTS}); ask HR to re-enrol you')
        correct = sum(1 for q, a in zip(qs, b['answers']) if q['correct_index'] == a)
        from hajiri.performance.common import js_round
        score = js_round((correct / len(qs)) * 10000) / 100
        if float(score).is_integer():
            score = int(score)
        passed = score >= (e['pass_mark'] or 60)
        execute('INSERT INTO lrn_attempt (enrollment_id, answers, score, passed) VALUES (%s, %s, %s, %s)', [e['id'], json.dumps(b['answers']), score, passed])
        execute('UPDATE lrn_enrollment SET score = GREATEST(coalesce(score, 0), %s), passed = coalesce(passed, false) OR %s WHERE id = %s',
                [score, passed, e['id']])
        # An online course is done when the assessment is passed; classroom courses also need the trainer's attendance.
        att = one('SELECT attended FROM lrn_enrollment WHERE id = %s', [e['id']])
        if passed and (e['delivery'] == 'online' or (att and att['attended'])):
            complete_enrollment(e['id'])
    return {'score': score, 'passed': passed, 'correct': correct, 'total': len(qs), 'attemptsLeft': MAX_ATTEMPTS - used - 1}


def course_feedback(user: AuthUser, eid: str, body: dict) -> None:
    b = parse(Obj({'rating': Int(min=1, max=5), 'comment': Str(trim=True, max=2000, nullish=True)}), body)
    e = load_enrollment(company_id_of(user), parse_uuid(str(eid)))
    if user.employee_id != e['employee_id']:
        raise NotFoundError('enrolment not found')
    if e['status'] != 'completed':
        raise ConflictError('rate the course after completing it')
    execute('UPDATE lrn_enrollment SET feedback_rating = %s, feedback_comment = %s WHERE id = %s', [b['rating'], b.get('comment'), e['id']])


def attach_certificate(user: AuthUser, eid: str, body: dict) -> None:
    """Attach an uploaded certificate (e.g. from an external provider); HR may set the issue and expiry dates."""
    from hajiri.files.store import attach_file
    b = parse(Obj({'fileId': Uuid(), 'issuedOn': IsoDate(optional=True), 'expiresOn': IsoDate(nullish=True)}), body)
    company_id = company_id_of(user)
    e = load_enrollment(company_id, parse_uuid(str(eid)))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    if e['status'] != 'completed':
        raise ConflictError('add a certificate after the course is completed')
    if ('issuedOn' in b or 'expiresOn' in b) and not has_role(user, 'hr'):
        raise ForbiddenError('only HR can set the issue and expiry dates of a certificate')
    if b.get('issuedOn') and b.get('expiresOn') and b['expiresOn'] < b['issuedOn']:
        raise BadRequestError('the certificate expires before it was issued')
    with transaction.atomic():
        attach_file(company_id=company_id, file_id=b['fileId'], user_id=user.id, purpose='training_certificate', attached_to=f"lrn_enrollment:{e['id']}")
        execute("""UPDATE lrn_enrollment SET certificate_file_id = %s, certificate_issued_on = coalesce(%s, certificate_issued_on),
                          certificate_expires_on = CASE WHEN %s THEN %s::date ELSE certificate_expires_on END, expiry_reminded_at = NULL WHERE id = %s""",
                [b['fileId'], b.get('issuedOn'), 'expiresOn' in b, b.get('expiresOn'), e['id']])
        audit('certificate', 'lrn_enrollment', actor_id=user.id, row_id=e['id'], after={'fileId': b['fileId']})


def certificate_file(user: AuthUser, eid: str) -> dict:
    from hajiri.files.store import read_file
    company_id = company_id_of(user)
    e = load_enrollment(company_id, parse_uuid(str(eid)))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    if not e['certificate_file_id']:
        raise NotFoundError('no certificate uploaded')
    return read_file(company_id, e['certificate_file_id'])


_CERT_CSS = """
@page{size:A4 landscape;margin:0}body{margin:0;font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;color:#17191e}
.page{box-sizing:border-box;width:297mm;height:210mm;padding:22mm;display:flex;flex-direction:column;justify-content:center;align-items:center;text-align:center;border:10px solid #12773a;outline:2px solid #12773a;outline-offset:-22px}
.co{font-size:14px;letter-spacing:.2em;text-transform:uppercase;color:#6b707b}h1{font-size:40px;margin:16px 0 4px;font-weight:600;letter-spacing:-.02em}.to{color:#6b707b;margin-top:24px}
.name{font-size:30px;font-weight:600;margin:6px 0;color:#12773a}.course{font-size:20px;margin:8px 0}.meta{color:#6b707b;margin-top:18px;font-size:13px}
.sign{display:flex;gap:120px;margin-top:56px}.sign div{border-top:1px solid #9aa0aa;padding-top:6px;width:200px;color:#6b707b;font-size:12px}"""


def certificate_html(user: AuthUser, eid: str) -> str:
    """Printable completion certificate generated by the system."""
    company_id = company_id_of(user)
    e = load_enrollment(company_id, parse_uuid(str(eid)))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    x = one("""SELECT e.status, em.name_en, em.emp_code, c.title, c.duration_hours, c.provider, e.certificate_issued_on, e.certificate_expires_on, e.score,
                      co.name_en AS company
                 FROM lrn_enrollment e JOIN employee em ON em.id = e.employee_id JOIN lrn_course c ON c.id = e.course_id JOIN company co ON co.id = e.company_id
                WHERE e.id = %s""", [e['id']])
    if x['status'] != 'completed':
        raise ConflictError('the certificate is available after completion')
    meta = ' · '.join(p for p in [f"{esc(fmt_number(x['duration_hours']))} hours" if x['duration_hours'] else None,
                                  f"Provided by {esc(x['provider'])}" if x['provider'] else None,
                                  f"Assessment score {esc(fmt_number(x['score']))}%" if x['score'] is not None else None] if p)
    valid = f" · Valid until {esc(bs_label(x['certificate_expires_on']))}" if x['certificate_expires_on'] else ''
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Certificate {esc(x['emp_code'])} {esc(x['title'])}</title><style>{_CERT_CSS}</style></head><body><div class="page">
<div class="co">{esc(x['company'])}</div><h1>Certificate of completion</h1><div class="to">This certifies that</div><div class="name">{esc(x['name_en'])}</div>
<div>has completed</div><div class="course">{esc(x['title'])}</div>
<div class="meta">{meta}</div>
<div class="meta">Issued {esc(bs_label(x['certificate_issued_on']))}{valid} · Reference {esc(e['id'][-8:].upper())}</div>
<div class="sign"><div>Trainer</div><div>HR</div></div></div></body></html>"""


# ---------------------------------------------------------------- reports

def expiring(user: AuthUser, days: int | str = 60) -> list[dict]:
    _require_hr(user)
    d = parse(Int(min=0, max=365, coerce=True, default=60), days if days not in (None, '') else None) if days not in (None, '') else 60
    out = rows(f"""{ENROLLMENT_SELECT} WHERE e.company_id = %s AND e.status = 'completed' AND e.certificate_expires_on IS NOT NULL
                     AND e.certificate_expires_on <= %s::date + %s::int AND em.status = 'active'
                     AND NOT EXISTS (SELECT 1 FROM lrn_enrollment n WHERE n.course_id = e.course_id AND n.employee_id = e.employee_id
                                      AND n.created_at > e.created_at AND n.status IN ('enrolled', 'completed'))
                   ORDER BY e.certificate_expires_on""", [company_id_of(user), npt_today(), d])
    return [_s(r, *_EID) for r in out]


def cost_report(user: AuthUser, query: dict) -> dict:
    """Training spend by course and by department, counting enrolled and completed seats in the period."""
    _require_hr(user)
    q = parse(Obj({'from': IsoDate(), 'to': IsoDate()}), query)
    if q['to'] < q['from']:
        raise BadRequestError('the period ends before it starts')
    base = """FROM lrn_enrollment e JOIN lrn_course c ON c.id = e.course_id
      LEFT JOIN employee_assignment a ON a.employee_id = e.employee_id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
      WHERE e.company_id = %s AND e.status IN ('enrolled', 'completed') AND (coalesce(e.completed_at, e.created_at) AT TIME ZONE 'Asia/Kathmandu')::date BETWEEN %s AND %s"""
    p = [company_id_of(user), q['from'], q['to']]
    by_course = rows(f"""SELECT c.id, c.code, c.title, count(*)::int AS seats, count(*) FILTER (WHERE e.status = 'completed')::int AS completed,
                                coalesce(sum(e.cost), 0)::text AS cost, round(avg(e.feedback_rating), 2)::text AS rating {base}
                          GROUP BY c.id, c.code, c.title ORDER BY sum(e.cost) DESC, c.title""", p)
    by_dept = rows(f"""SELECT coalesce(ou.name_en, 'No department') AS department, count(*)::int AS seats, coalesce(sum(e.cost), 0)::text AS cost {base}
                        GROUP BY 1 ORDER BY sum(e.cost) DESC""", p)
    from decimal import Decimal
    total = sum((Decimal(x['cost']) for x in by_course), Decimal('0'))
    return {'byCourse': [_s(r, 'id') for r in by_course], 'byDepartment': by_dept, 'total': f'{total:.2f}'}


_ = num
