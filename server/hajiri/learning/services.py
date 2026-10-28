"""
Learning: course catalogue, sessions, assessments, enrolments (self-nomination through approvals, assignment,
mandatory training), certificates, expiry reminders and spend (port of apps/api/src/modules/learning/service.ts and
routes.ts). Bodies use the TS camelCase shape and are validated with the serializers in serializers.py.
"""
from __future__ import annotations

import calendar
from datetime import date, datetime, timedelta
from decimal import ROUND_HALF_UP, Decimal

from django.db import IntegrityError, transaction
from django.db.models import Avg, Count, DecimalField, Exists, F, OuterRef, Q, Subquery, Sum, TextField, Value
from django.db.models.functions import Coalesce, Greatest, Now, TruncDate
from django.template.loader import render_to_string

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import NPT, npt_today
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid, exists_in_company
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import Employee, LrnAttempt, LrnCourse, LrnEnrollment, LrnQuestion, LrnSession
from hajiri.notifications.service import notify, notify_role, user_id_for_employee
from hajiri.performance.common import (SubqueryCount, current_assignments, employee_in_company, first_time, fmt_number,
                                       is_above_in_chain, npt_day_bounds, plain_row, plain_rows, round_half_up,
                                       subordinate_ids)
from hajiri.workflow.service import DecisionContext, cancel_workflow, register_handler, start_workflow

from . import serializers as s
from .filters import EnrollmentFilter

MAX_ATTEMPTS = 5
ENROLL_STATUSES = s.ENROLL_STATUSES
Status, Source = LrnEnrollment.Status, LrnEnrollment.Source
OPEN = (Status.PENDING_APPROVAL, Status.ENROLLED)
SEAT_TAKEN = (Status.PENDING_APPROVAL, Status.ENROLLED, Status.COMPLETED)


def add_months(iso: str, months: int) -> str:
    """'YYYY-MM-DD' plus whole months, clamped to the end of a shorter month (31 Jan + 1 month = 28/29 Feb)."""
    d = date.fromisoformat(str(iso)[:10])
    total = (d.month - 1) + months
    ty, tm = d.year + total // 12, total % 12 + 1
    return date(ty, tm, min(d.day, calendar.monthrange(ty, tm)[1])).isoformat()


def _status_text(st: str) -> str:
    return st.replace('_', ' ', 1)


def _instant(iso: str) -> datetime:
    return datetime.fromisoformat(iso.replace('Z', '+00:00'))


def complete_enrollment(enrollment_id: str, attended: bool | None = None) -> None:
    """Mark an enrolment complete and issue the certificate dates from the course's validity."""
    e = (LrnEnrollment.objects.select_for_update(of=('self',)).select_related('course').filter(pk=enrollment_id).first())
    if not e:
        raise NotFoundError('enrolment not found')
    if e.status == Status.COMPLETED:
        return
    if e.status != Status.ENROLLED:
        raise ConflictError(f'this enrolment is {_status_text(e.status)}')
    today = npt_today().isoformat()
    changes = {'status': Status.COMPLETED, 'completed_at': Now(), 'certificate_issued_on': today, 'expiry_reminded_at': None,
               'certificate_expires_on': add_months(today, e.course.validity_months) if e.course.validity_months else None}
    if attended is not None:
        changes['attended'] = attended
    LrnEnrollment.objects.filter(pk=e.pk).update(**changes)
    emp = str(e.employee_id)
    notify(company_id=str(e.company_id), user_id=user_id_for_employee(emp), employee_id=emp, template='learning.completed',
           title='Training completed', body=f'You completed {e.course.title}. Your certificate is under Growth, Learning.',
           payload={'enrollmentId': str(enrollment_id)})


def _enroll_unless_open(**fields) -> bool:
    """Insert an enrolment unless the person already has an open one (pending or enrolled) for the course; True when
    inserted. Only open enrolments are unique (lrn_enrollment_open_idx), so a completed or cancelled one does not stop
    a new enrolment, e.g. a refresher."""
    try:
        with transaction.atomic():
            LrnEnrollment.objects.create(**fields)
    except IntegrityError as err:
        if getattr(err.__cause__, 'sqlstate', None) == '23505':
            return False
        raise
    return True


def create_review_nominations(company_id: str, employee_id: str, review_id: str, course_ids: list[str], actor_id: str) -> int:
    """Training needs from a released review become enrolments the employee can see."""
    n = 0
    for course_id in course_ids:
        c = LrnCourse.objects.for_company(company_id).filter(pk=course_id, active=True).first()
        if not c:
            continue
        if _enroll_unless_open(company_id=company_id, course_id=c.id, employee_id=employee_id, source=Source.REVIEW,
                               status=Status.ENROLLED, review_id=review_id, assigned_by_id=actor_id, cost=c.cost):
            n += 1
            notify(company_id=company_id, user_id=user_id_for_employee(employee_id), employee_id=employee_id, template='learning.assigned',
                   title='Training assigned', body=f'From your review: {c.title}.', payload={'courseId': str(course_id)})
    return n


def apply_nomination_decision(enrollment_id: str, state: str, ctx: DecisionContext) -> None:
    e = LrnEnrollment.objects.select_for_update(of=('self',)).select_related('course').filter(pk=enrollment_id).first()
    if not e:
        raise NotFoundError('enrolment not found')
    if e.status != Status.PENDING_APPROVAL:
        raise ConflictError(f'nomination already {e.status}')
    LrnEnrollment.objects.filter(pk=e.pk).update(status=Status.ENROLLED if state == 'approved' else Status.REJECTED)
    emp = str(e.employee_id)
    notify(company_id=str(e.company_id), user_id=user_id_for_employee(emp), employee_id=emp, template=f'learning.nomination_{state}',
           title='Training approved' if state == 'approved' else 'Training not approved',
           body=f"{e.course.title}{f' ({ctx.reason})' if ctx.reason else ''}.", payload={'enrollmentId': str(enrollment_id)})


register_handler('training_nomination', apply_nomination_decision)


def _no_newer_enrollment(**extra):
    """No later enrolment of the same person in the same course (a refresher replaces the old certificate)."""
    return ~Exists(LrnEnrollment.objects.filter(course_id=OuterRef('course_id'), employee_id=OuterRef('employee_id'),
                                                created_at__gt=OuterRef('created_at'), **extra))


def learning_reminders(now=None) -> dict:
    """
    Daily: certificates of mandatory or expiring courses that run out within 30 days (employee and HR are told once per
    certificate), and reminders the day before a scheduled session.
    """
    today = npt_today()
    exp = list(LrnEnrollment.objects
               .filter(status=Status.COMPLETED, certificate_expires_on__isnull=False, expiry_reminded_at__isnull=True,
                       employee__status=Employee.Status.ACTIVE, certificate_expires_on__lte=today + timedelta(days=30))
               .filter(_no_newer_enrollment())
               .values('id', 'company_id', 'employee_id', 'certificate_expires_on', title=F('course__title'), name_en=F('employee__name_en')))
    for e in exp:
        past = e['certificate_expires_on'] < today
        body = f"{e['title']} certificate {'expired' if past else 'expires'} on {bs_label(e['certificate_expires_on'])}."
        title = 'Certificate expired' if past else 'Certificate expiring'
        emp, co = str(e['employee_id']), str(e['company_id'])
        notify(company_id=co, user_id=user_id_for_employee(emp), employee_id=emp, template='learning.certificate_expiring', title=title,
               body=f'{body} Ask HR for a refresher session.', payload={'enrollmentId': str(e['id'])})
        notify_role(co, 'hr', employee_id=emp, template='learning.certificate_expiring', title=title, body=f"{e['name_en']}: {body}",
                    payload={'enrollmentId': str(e['id'])})
        LrnEnrollment.objects.filter(pk=e['id']).update(expiry_reminded_at=Now())
    start, end = npt_day_bounds(today + timedelta(days=1))
    sess = (LrnEnrollment.objects.filter(status=Status.ENROLLED, session__status=LrnSession.Status.SCHEDULED,
                                         session__starts_at__gte=start, session__starts_at__lt=end)
            .values('id', 'company_id', 'employee_id', title=F('course__title'), venue=F('session__venue')))
    sessions = 0
    for e in sess:
        if not first_time(f"session:{e['id']}:{today.isoformat()}"):
            continue
        sessions += 1
        emp = str(e['employee_id'])
        notify(company_id=str(e['company_id']), user_id=user_id_for_employee(emp), employee_id=emp, template='learning.session_tomorrow',
               title='Training tomorrow', body=f"{e['title']}{' at ' + e['venue'] if e['venue'] else ''}.", payload={'enrollmentId': str(e['id'])})
    return {'expiring': len(exp), 'sessions': sessions}


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


# ---------------------------------------------------------------- catalogue

COURSE_FIELDS = {'code': 'code', 'title': 'title', 'description': 'description', 'category': 'category', 'delivery': 'delivery',
                 'source': 'source', 'provider': 'provider', 'durationHours': 'duration_hours', 'url': 'url', 'cost': 'cost',
                 'mandatory': 'mandatory', 'mandatoryFor': 'mandatory_for', 'validityMonths': 'validity_months', 'passMark': 'pass_mark',
                 'active': 'active'}
SESSION_FIELDS = {'startsAt': 'starts_at', 'endsAt': 'ends_at', 'venue': 'venue', 'trainer': 'trainer', 'capacity': 'capacity',
                  'status': 'status'}


def _changes(b: dict, mapping: dict) -> dict:
    out = {col: b[k] for k, col in mapping.items() if k in b}
    for col in ('starts_at', 'ends_at'):
        if col in out:
            out[col] = _instant(out[col])
    return out


def _courses(user: AuthUser):
    qs = LrnCourse.objects.for_company(company_id_of(user))
    return qs if has_role(user, 'hr') else qs.filter(active=True)


def _seats_taken():
    return SubqueryCount(LrnEnrollment.objects.filter(session_id=OuterRef('pk'), status__in=SEAT_TAKEN))


def list_courses(user: AuthUser) -> list[dict]:
    out = (_courses(user)
           .annotate(questions=SubqueryCount(LrnQuestion.objects.filter(course_id=OuterRef('pk'))),
                     completed=SubqueryCount(LrnEnrollment.objects.filter(course_id=OuterRef('pk'), status=Status.COMPLETED)),
                     enrolled=SubqueryCount(LrnEnrollment.objects.filter(course_id=OuterRef('pk'), status__in=OPEN)),
                     next_session=Subquery(LrnSession.objects.filter(course_id=OuterRef('pk'), status=LrnSession.Status.SCHEDULED,
                                                                     starts_at__gt=Now()).order_by('starts_at').values('starts_at')[:1]))
           .order_by('-mandatory', 'title').values())
    return plain_rows(out)


def course_detail(user: AuthUser, course_id: str) -> dict:
    hr = has_role(user, 'hr')
    c = plain_row(_courses(user).filter(pk=parse_uuid(course_id)).values().first())
    if not c:
        raise NotFoundError('course not found')
    sessions = LrnSession.objects.filter(course_id=c['id']).annotate(taken=_seats_taken()).order_by('-starts_at').values()
    # Answers stay on the server; HR sees them to edit the assessment.
    qs = list(LrnQuestion.objects.filter(course_id=c['id']).order_by('sort', 'id')
              .values('id', 'prompt', 'options', *(('correct_index',) if hr else ())))
    return {'course': c, 'sessions': plain_rows(sessions), 'questions': plain_rows(qs) if hr else [], 'questionCount': len(qs)}


def create_course(user: AuthUser, body: dict) -> str:
    _require_hr(user)
    b = validate(s.CourseIn, body)
    c = LrnCourse.objects.create(company_id=company_id_of(user), **_changes(b, COURSE_FIELDS))
    return str(c.id)


def update_course(user: AuthUser, course_id: str, body: dict) -> None:
    _require_hr(user)
    b = validate(s.CourseIn, body, partial=True)
    changes = _changes(b, COURSE_FIELDS)
    if changes and not LrnCourse.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(course_id)).update(**changes):
        raise NotFoundError('course not found')


def save_questions(user: AuthUser, course_id: str, body: dict) -> None:
    """Replace the course's multiple-choice assessment."""
    _require_hr(user)
    b = validate(s.QuestionsIn, body)
    cid = parse_uuid(course_id)
    with transaction.atomic():
        course = LrnCourse.objects.for_company(company_id_of(user)).filter(pk=cid)
        if not course.exists():
            raise NotFoundError('course not found')
        LrnQuestion.objects.filter(course_id=cid).delete()
        LrnQuestion.objects.bulk_create([LrnQuestion(course_id=cid, prompt=q['prompt'], options=list(q['options']),
                                                     correct_index=q['correctIndex'], sort=sort)
                                         for sort, q in enumerate(b['questions'])])
        if b['questions']:
            course.update(pass_mark=Coalesce('pass_mark', Value(60)))


def add_session(user: AuthUser, course_id: str, body: dict) -> str:
    _require_hr(user)
    b = validate(s.SessionIn, body)
    fields = _changes(b, SESSION_FIELDS)
    if fields['ends_at'] <= fields['starts_at']:
        raise BadRequestError('the session ends before it starts')
    cid = parse_uuid(course_id)
    company_id = company_id_of(user)
    if not exists_in_company(LrnCourse, company_id, pk=cid):
        raise NotFoundError('course not found')
    return str(LrnSession.objects.create(company_id=company_id, course_id=cid, **fields).id)


def update_session(user: AuthUser, session_id: str, body: dict) -> str:
    """Change a session; returns its course id."""
    _require_hr(user)
    b = validate(s.SessionIn, body, partial=True)
    sessions = LrnSession.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(session_id))
    course_id = sessions.values_list('course_id', flat=True).first()
    if not course_id:
        raise NotFoundError('session not found')
    changes = _changes(b, SESSION_FIELDS)
    if changes:
        sessions.update(**changes)
    return str(course_id)


# ---------------------------------------------------------------- enrolments

ENROLLMENT_VALUES = ('id', 'course_id', 'session_id', 'employee_id', 'source', 'status', 'attended', 'completed_at', 'score', 'passed',
                     'feedback_rating', 'feedback_comment', 'certificate_file_id', 'certificate_issued_on', 'certificate_expires_on',
                     'cost', 'reason', 'created_at')


def _questions_of_course():
    return SubqueryCount(LrnQuestion.objects.filter(course_id=OuterRef('course_id')))


def _enrollment_rows(qs) -> list[dict]:
    return plain_rows(qs.values(
        *ENROLLMENT_VALUES, code=F('course__code'), title=F('course__title'), delivery=F('course__delivery'),
        mandatory=F('course__mandatory'), pass_mark=F('course__pass_mark'), validity_months=F('course__validity_months'),
        starts_at=F('session__starts_at'), ends_at=F('session__ends_at'), venue=F('session__venue'), name_en=F('employee__name_en'),
        emp_code=F('employee__emp_code'), questions=_questions_of_course(),
        attempts=SubqueryCount(LrnAttempt.objects.filter(enrollment_id=OuterRef('pk')))))


def load_enrollment(company_id: str, eid: str) -> dict:
    r = (LrnEnrollment.objects.for_company(company_id).filter(pk=as_uuid(eid))
         .values('id', 'company_id', 'course_id', 'session_id', 'employee_id', 'status', 'workflow_instance_id', 'certificate_file_id',
                 delivery=F('course__delivery'), pass_mark=F('course__pass_mark'), title=F('course__title'),
                 questions=_questions_of_course()).first())
    if not r:
        raise NotFoundError('enrolment not found')
    return plain_row(r)


def enrollment_role(user: AuthUser, e: dict) -> str | None:
    """The learner, their managers and HR may see an enrolment; only managers and HR may manage it."""
    if user.employee_id == e['employee_id']:
        return 'self'
    if is_above_in_chain(user, e['employee_id']):
        return 'manager'
    return None


def _session_has_room(session_id: str, course_id: str, company_id: str) -> None:
    sess = (LrnSession.objects.for_company(company_id).filter(pk=session_id, course_id=course_id)
            .annotate(taken=_seats_taken()).values('capacity', 'status', 'taken').first())
    if not sess:
        raise NotFoundError('session not found')
    if sess['status'] != LrnSession.Status.SCHEDULED:
        raise ConflictError('this session is not open')
    if sess['capacity'] is not None and sess['taken'] >= sess['capacity']:
        raise ConflictError('this session is full')


def list_enrollments(user: AuthUser, query: dict | None = None) -> list[dict]:
    q = plain(validate(s.EnrollmentQueryIn, query or {}))
    qs = EnrollmentFilter({k: q[k] for k in ('courseId', 'status') if q.get(k)},
                          queryset=LrnEnrollment.objects.for_company(company_id_of(user))).qs
    if q['scope'] == 'mine':
        if not user.employee_id:
            return []
        return _enrollment_rows(qs.filter(employee_id=user.employee_id).order_by('-created_at'))
    if q['scope'] == 'all' or not user.employee_id:
        if not has_role(user, 'hr'):
            raise ForbiddenError('requires HR')
        return _enrollment_rows(qs.order_by('-created_at')[:1000])
    return _enrollment_rows(qs.filter(employee_id__in=subordinate_ids(user.employee_id)).order_by('-created_at')[:1000])


def _active_course(company_id: str, course_id: str) -> LrnCourse:
    c = LrnCourse.objects.for_company(company_id).filter(pk=course_id, active=True).first()
    if not c:
        raise NotFoundError('course not found')
    return c


def self_enroll(user: AuthUser, body: dict) -> str:
    """Ask to join a course; the manager approves through the approvals inbox."""
    b = plain(validate(s.SelfEnrollIn, body))
    company_id = company_id_of(user)
    if not user.employee_id:
        raise ForbiddenError('this account is not linked to an employee')
    c = _active_course(company_id, b['courseId'])
    if b.get('sessionId'):
        _session_has_room(b['sessionId'], b['courseId'], company_id)
    with transaction.atomic():
        try:
            with transaction.atomic():
                e = LrnEnrollment.objects.create(company_id=company_id, course_id=c.id, session_id=b.get('sessionId'),
                                                 employee_id=user.employee_id, source=Source.SELF, status=Status.PENDING_APPROVAL,
                                                 reason=b.get('reason'), cost=c.cost)
        except IntegrityError as err:
            if getattr(err.__cause__, 'sqlstate', None) == '23505':
                raise ConflictError('you are already enrolled or waiting for approval for this course') from None
            raise
        eid = str(e.id)
        reason = b.get('reason')
        inst = start_workflow(company_id=company_id, object_type='training_nomination', object_id=eid, employee_id=user.employee_id,
                              title='Training request', body=f"{user.name} asked to join {c.title}{': ' + reason if reason else ''}")
        LrnEnrollment.objects.filter(pk=eid).update(workflow_instance_id=inst['id'])
    return eid


def assign(user: AuthUser, body: dict) -> dict:
    """HR assigns anyone; a manager assigns their team. Assigned enrolments need no approval."""
    b = plain(validate(s.AssignIn, body))
    company_id = company_id_of(user)
    if not has_role(user, 'hr', 'manager'):
        raise ForbiddenError('only managers and HR assign training')
    c = _active_course(company_id, b['courseId'])
    added = skipped = 0
    with transaction.atomic():
        for emp in dict.fromkeys(b['employeeIds']):
            employee_in_company(company_id, emp)
            if not is_above_in_chain(user, emp):
                raise ForbiddenError('you can assign training only to your team')
            if b.get('sessionId'):
                _session_has_room(b['sessionId'], b['courseId'], company_id)
            if _enroll_unless_open(company_id=company_id, course_id=c.id, session_id=b.get('sessionId'), employee_id=emp,
                                   source=Source.ASSIGNED, status=Status.ENROLLED, assigned_by_id=user.id, cost=c.cost):
                added += 1
                notify(company_id=company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='learning.assigned',
                       title='Training assigned', body=f'{user.name} enrolled you in {c.title}.', payload={'courseId': b['courseId']})
            else:
                skipped += 1
    return {'added': added, 'skipped': skipped}


def assign_mandatory(user: AuthUser, course_id: str) -> int:
    """Enrol everyone a mandatory course applies to who has no open enrolment and no certificate still valid."""
    _require_hr(user)
    cid = parse_uuid(course_id)
    company_id = company_id_of(user)
    course = _active_course(company_id, cid)
    if not course.mandatory:
        raise BadRequestError('mark the course mandatory first')
    soon = npt_today() + timedelta(days=30)
    covered = LrnEnrollment.objects.filter(employee_id=OuterRef('pk'), course_id=cid).filter(
        Q(status__in=OPEN) | Q(status=Status.COMPLETED) & (Q(certificate_expires_on__isnull=True) | Q(certificate_expires_on__gt=soon)))
    with transaction.atomic():
        emps = Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE).exclude(Exists(covered))
        if course.mandatory_for:
            emps = emps.filter(designation__in=course.mandatory_for)
        emps = [str(e) for e in emps.values_list('id', flat=True)]
        LrnEnrollment.objects.bulk_create(
            [LrnEnrollment(company_id=company_id, course_id=cid, employee_id=emp, source=Source.MANDATORY, status=Status.ENROLLED,
                           assigned_by_id=user.id, cost=course.cost) for emp in emps], ignore_conflicts=True)
        for emp in emps:
            notify(company_id=company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='learning.assigned',
                   title='Mandatory training', body=f'You are enrolled in {course.title}.', payload={'courseId': cid})
    return len(emps)


def cancel(user: AuthUser, eid: str) -> None:
    e = load_enrollment(company_id_of(user), parse_uuid(eid))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    if e['status'] not in OPEN:
        raise ConflictError(f"this enrolment is {_status_text(e['status'])}")
    with transaction.atomic():
        LrnEnrollment.objects.filter(pk=e['id']).update(status=Status.CANCELLED)
        if e['status'] == Status.PENDING_APPROVAL:
            cancel_workflow('training_nomination', e['id'])


def record_attendance(user: AuthUser, eid: str, body: dict) -> None:
    """Trainer's register: attended completes the course (unless an assessment is still to pass); absent is a no-show."""
    b = validate(s.AttendanceIn, body)
    e = load_enrollment(company_id_of(user), parse_uuid(eid))
    role = enrollment_role(user, e)
    if not role:
        raise NotFoundError('enrolment not found')
    if role != 'manager':
        raise ForbiddenError('managers and HR record attendance')
    if e['status'] != Status.ENROLLED:
        raise ConflictError(f"this enrolment is {_status_text(e['status'])}")
    with transaction.atomic():
        if not b['attended']:
            LrnEnrollment.objects.filter(pk=e['id']).update(attended=False, status=Status.NO_SHOW)
        elif e['questions'] > 0:
            LrnEnrollment.objects.filter(pk=e['id']).update(attended=True)
        else:
            complete_enrollment(e['id'], attended=True)
        audit('attendance', 'lrn_enrollment', actor_id=user.id, row_id=e['id'], after=b)


def complete(user: AuthUser, eid: str) -> None:
    """Online courses without an assessment are completed by the learner; managers and HR may complete any."""
    e = load_enrollment(company_id_of(user), parse_uuid(eid))
    role = enrollment_role(user, e)
    if not role:
        raise NotFoundError('enrolment not found')
    if role == 'self':
        if e['questions'] > 0:
            raise ConflictError('pass the assessment to complete this course')
        if e['delivery'] != LrnCourse.Delivery.ONLINE:
            raise ForbiddenError('classroom attendance is recorded by the trainer or HR')
    with transaction.atomic():
        complete_enrollment(e['id'])


def _my_enrollment(user: AuthUser, eid: str) -> dict:
    e = load_enrollment(company_id_of(user), parse_uuid(eid))
    if user.employee_id != e['employee_id']:
        raise NotFoundError('enrolment not found')
    return e


def get_enrollment(user: AuthUser, eid: str, *, learner_only: bool = False) -> dict:
    """An enrolment the person may see (only their own with learner_only), for the forms that act on it."""
    if learner_only:
        return _my_enrollment(user, eid)
    e = load_enrollment(company_id_of(user), parse_uuid(eid))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    return e


def assessment(user: AuthUser, eid: str) -> dict:
    e = _my_enrollment(user, eid)
    qs = LrnQuestion.objects.filter(course_id=e['course_id']).order_by('sort', 'id').values('id', 'prompt', 'options')
    attempts = list(LrnAttempt.objects.filter(enrollment_id=e['id']).order_by('-created_at').values('score', 'passed', 'created_at'))
    return {'title': e['title'], 'passMark': e['pass_mark'], 'questions': plain_rows(qs), 'attempts': attempts,
            'attemptsLeft': max(0, MAX_ATTEMPTS - len(attempts))}


def attempt(user: AuthUser, eid: str, body: dict) -> dict:
    b = validate(s.AttemptIn, body)
    with transaction.atomic():
        e = _my_enrollment(user, eid)
        row = LrnEnrollment.objects.filter(pk=e['id'])
        # Lock it (one attempt at a time) and read the status under the lock, so a parallel attempt that completed the
        # course is seen.
        status = row.select_for_update().values_list('status', flat=True).get()
        if status != Status.ENROLLED:
            raise ConflictError('you already completed this course' if status == Status.COMPLETED
                                else f"this enrolment is {_status_text(status)}")
        correct_answers = list(LrnQuestion.objects.filter(course_id=e['course_id']).order_by('sort', 'id')
                               .values_list('correct_index', flat=True))
        if not correct_answers:
            raise BadRequestError('this course has no assessment')
        if len(b['answers']) != len(correct_answers):
            raise BadRequestError(f'answer all {len(correct_answers)} questions')
        used = LrnAttempt.objects.filter(enrollment_id=e['id']).count()
        if used >= MAX_ATTEMPTS:
            raise ConflictError(f'no attempts left (maximum {MAX_ATTEMPTS}); ask HR to re-enrol you')
        correct = sum(1 for want, got in zip(correct_answers, b['answers']) if want == got)
        score = round_half_up(correct / len(correct_answers) * 100, 2)
        if float(score).is_integer():
            score = int(score)
        passed = score >= (e['pass_mark'] or 60)
        LrnAttempt.objects.create(enrollment_id=e['id'], answers=list(b['answers']), score=score, passed=passed)
        row.update(score=Greatest(Coalesce('score', Value(Decimal(0))), Value(Decimal(str(score))), output_field=DecimalField()),
                   passed=Value(True) if passed else Coalesce('passed', Value(False)))
        # An online course is done when the assessment is passed; classroom courses also need the trainer's attendance.
        attended = row.values_list('attended', flat=True).get()
        if passed and (e['delivery'] == LrnCourse.Delivery.ONLINE or attended):
            complete_enrollment(e['id'])
    return {'score': score, 'passed': passed, 'correct': correct, 'total': len(correct_answers), 'attemptsLeft': MAX_ATTEMPTS - used - 1}


def course_feedback(user: AuthUser, eid: str, body: dict) -> None:
    b = validate(s.CourseFeedbackIn, body)
    e = _my_enrollment(user, eid)
    if e['status'] != Status.COMPLETED:
        raise ConflictError('rate the course after completing it')
    LrnEnrollment.objects.filter(pk=e['id']).update(feedback_rating=b['rating'], feedback_comment=b.get('comment'))


def attach_certificate(user: AuthUser, eid: str, body: dict) -> None:
    """Attach an uploaded certificate (e.g. from an external provider); HR may set the issue and expiry dates."""
    from hajiri.files.store import attach_file
    b = plain(validate(s.CertificateIn, body))
    company_id = company_id_of(user)
    e = load_enrollment(company_id, parse_uuid(eid))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    if e['status'] != Status.COMPLETED:
        raise ConflictError('add a certificate after the course is completed')
    if ('issuedOn' in b or 'expiresOn' in b) and not has_role(user, 'hr'):
        raise ForbiddenError('only HR can set the issue and expiry dates of a certificate')
    if b.get('issuedOn') and b.get('expiresOn') and b['expiresOn'] < b['issuedOn']:
        raise BadRequestError('the certificate expires before it was issued')
    changes = {'certificate_file_id': b['fileId'], 'expiry_reminded_at': None}
    if b.get('issuedOn'):
        changes['certificate_issued_on'] = b['issuedOn']
    if 'expiresOn' in b:
        changes['certificate_expires_on'] = b['expiresOn']
    with transaction.atomic():
        attach_file(company_id=company_id, file_id=b['fileId'], user_id=user.id, purpose='training_certificate',
                    attached_to=f"lrn_enrollment:{e['id']}")
        LrnEnrollment.objects.filter(pk=e['id']).update(**changes)
        audit('certificate', 'lrn_enrollment', actor_id=user.id, row_id=e['id'], after={'fileId': b['fileId']})


def certificate_file(user: AuthUser, eid: str) -> dict:
    from hajiri.files.store import read_file
    company_id = company_id_of(user)
    e = load_enrollment(company_id, parse_uuid(eid))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    if not e['certificate_file_id']:
        raise NotFoundError('no certificate uploaded')
    return read_file(company_id, e['certificate_file_id'])


def certificate_html(user: AuthUser, eid: str) -> str:
    """Printable completion certificate generated by the system."""
    company_id = company_id_of(user)
    e = load_enrollment(company_id, parse_uuid(eid))
    if not enrollment_role(user, e):
        raise NotFoundError('enrolment not found')
    x = (LrnEnrollment.objects.filter(pk=e['id'])
         .values('status', 'certificate_issued_on', 'certificate_expires_on', 'score', name_en=F('employee__name_en'),
                 emp_code=F('employee__emp_code'), title=F('course__title'), duration_hours=F('course__duration_hours'),
                 provider=F('course__provider'), company_name=F('company__name_en')).get())
    if x['status'] != Status.COMPLETED:
        raise ConflictError('the certificate is available after completion')
    meta = [p for p in [f"{fmt_number(x['duration_hours'])} hours" if x['duration_hours'] else None,
                        f"Provided by {x['provider']}" if x['provider'] else None,
                        f"Assessment score {fmt_number(x['score'])}%" if x['score'] is not None else None] if p]
    return render_to_string('learning/certificate.html', {
        'x': x, 'meta': ' · '.join(meta), 'issued': bs_label(x['certificate_issued_on']),
        'valid_until': bs_label(x['certificate_expires_on']) if x['certificate_expires_on'] else None,
        'reference': e['id'][-8:].upper()})


# ---------------------------------------------------------------- reports

def expiring(user: AuthUser, days: int | str = 60) -> list[dict]:
    _require_hr(user)
    d = validate(s.ExpiringIn, {'days': days})['days'] if days not in (None, '') else 60
    qs = (LrnEnrollment.objects.for_company(company_id_of(user))
          .filter(status=Status.COMPLETED, certificate_expires_on__isnull=False, employee__status=Employee.Status.ACTIVE,
                  certificate_expires_on__lte=npt_today() + timedelta(days=d))
          .filter(_no_newer_enrollment(status__in=(Status.ENROLLED, Status.COMPLETED)))
          .order_by('certificate_expires_on'))
    return _enrollment_rows(qs)


def _money(v) -> str:
    return f'{Decimal(v or 0):.2f}'


def cost_report(user: AuthUser, query: dict) -> dict:
    """Training spend by course and by department, counting enrolled and completed seats in the period."""
    _require_hr(user)
    q = validate(s.PeriodIn, query)
    if q['to'] < q['from']:
        raise BadRequestError('the period ends before it starts')
    base = (LrnEnrollment.objects.for_company(company_id_of(user)).filter(status__in=(Status.ENROLLED, Status.COMPLETED))
            .annotate(day=TruncDate(Coalesce('completed_at', 'created_at'), tzinfo=NPT)).filter(day__range=(q['from'], q['to'])))
    totals = {'seats': Count('id'), 'spend': Coalesce(Sum('cost'), Value(Decimal(0)))}
    by_course = []
    for r in (base.values('course_id', 'course__code', 'course__title')
              .annotate(**totals, done=Count('id', filter=Q(status=Status.COMPLETED)),
                        rating=Avg('feedback_rating', output_field=DecimalField()))
              .order_by('-spend', 'course__title')):
        rating = None if r['rating'] is None else str(Decimal(r['rating']).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
        by_course.append({'id': str(r['course_id']), 'code': r['course__code'], 'title': r['course__title'], 'seats': r['seats'],
                          'completed': r['done'], 'cost': _money(r['spend']), 'rating': rating})
    department = Coalesce(Subquery(current_assignments().filter(employee_id=OuterRef('employee_id')).values('org_unit__name_en')[:1]),
                          Value('No department'), output_field=TextField())
    by_dept = [{'department': r['department'], 'seats': r['seats'], 'cost': _money(r['spend'])}
               for r in base.annotate(department=department).values('department').annotate(**totals).order_by('-spend')]
    total = sum((Decimal(x['cost']) for x in by_course), Decimal('0'))
    return {'byCourse': by_course, 'byDepartment': by_dept, 'total': f'{total:.2f}'}
