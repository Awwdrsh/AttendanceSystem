"""
Competency framework and review cycles: self, manager and peer (360) reviews, calibration, release and
acknowledgement, and the printable summary (port of apps/api/src/modules/performance/reviews.ts).
"""
from __future__ import annotations

from django.db import transaction
from django.db.models import Case, Count, Exists, F, IntegerField, OuterRef, Q, Subquery, TextField, Value, When
from django.db.models.functions import Coalesce, Now
from django.template.loader import render_to_string

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.collation import sorted_by_name
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.scoping import as_uuid
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (Employee, LrnCourse, PerfCompetency, PerfCycle, PerfGoal, PerfJournal, PerfPeer, PerfReview,
                              PerfReviewItem)
from hajiri.notifications.service import notify, notify_role, user_id_for_employee

from . import serializers as s
from .common import (current_assignments, fmt_number, is_above_in_chain, num, overall_score, plain_row, plain_rows, round2,
                     round_half_up)

Phase = PerfCycle.Status
PeerStatus = PerfPeer.Status
ItemType = PerfReviewItem.ItemType
PHASES = tuple(Phase.values)
PHASE_LABEL = dict(Phase.choices)
DEFAULT_LABELS = ['Needs improvement', 'Partly meets', 'Meets expectations', 'Exceeds expectations', 'Outstanding']
OPEN_PHASES = (Phase.SELF_REVIEW, Phase.MANAGER_REVIEW, Phase.PEER_REVIEW)


def peer_aggregate(peers: list[dict], minimum: int) -> dict:
    """Peer responses are shown to the reviewed employee only as an anonymous aggregate of at least `minimum` responses."""
    done = [p for p in peers if p['status'] == PeerStatus.SUBMITTED]
    if len(done) < minimum:
        return {'count': len(done), 'shown': False}
    rated = [p for p in done if p['rating'] is not None]

    def sort_text(xs):
        return sorted_by_name(x for x in xs if x and x.strip())
    return {
        'count': len(done), 'shown': True,
        'averageRating': round2(sum(p['rating'] for p in rated) / len(rated)) if rated else None,
        'strengths': sort_text(p['strengths'] for p in done), 'improvements': sort_text(p['improvements'] for p in done),
    }


CYCLE_VALUES = ('id', 'company_id', 'name', 'period_start', 'period_end', 'status', 'goals_weight', 'competency_weight', 'rating_labels',
                'peer_enabled', 'peer_min', 'self_due', 'manager_due', 'peer_due', 'calibration_due', 'filter', 'created_by_id',
                'created_at', 'launched_at', 'released_at')
REVIEW_VALUES = ('id', 'cycle_id', 'company_id', 'employee_id', 'manager_employee_id', 'self_summary', 'self_submitted_at', 'self_score',
                 'manager_summary', 'manager_submitted_at', 'manager_score', 'recommendation', 'final_rating', 'calibrated_by_id',
                 'calibration_reason', 'released_at', 'acknowledged_at', 'employee_comment', 'created_at')


def load_cycle(company_id: str, cycle_id: str, lock: bool = False) -> dict:
    qs = PerfCycle.objects.for_company(company_id).filter(pk=as_uuid(cycle_id))
    if lock:
        qs = qs.select_for_update()
    r = qs.values(*CYCLE_VALUES).first()
    if not r:
        raise NotFoundError('review cycle not found')
    return plain_row(r)


def load_review(company_id: str, review_id: str) -> dict:
    rv = plain_row(PerfReview.objects.for_company(company_id).filter(pk=as_uuid(review_id)).values(*REVIEW_VALUES).first())
    if not rv:
        raise NotFoundError('review not found')
    return {**rv, 'cycle': load_cycle(company_id, rv['cycle_id'])}


def viewer_role(user: AuthUser, rv: dict) -> str | None:
    """'self' for the employee, 'manager' for the reviewing manager or anyone above, 'hr' for HR; otherwise no access."""
    if user.employee_id and user.employee_id == rv['employee_id']:
        return 'self'
    if has_role(user, 'hr'):
        return 'hr'
    if user.employee_id and rv['manager_employee_id'] == user.employee_id:
        return 'manager'
    if is_above_in_chain(user, rv['employee_id']):
        return 'manager'
    return None


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


def _changes(b: dict, mapping: dict[str, str]) -> dict:
    return {col: b[k] for k, col in mapping.items() if k in b}


def _department():
    """The current department name of the row's employee."""
    return Subquery(current_assignments().filter(employee_id=OuterRef('employee_id')).values('org_unit__name_en')[:1])


# ---------------------------------------------------------------- competency framework

COMPETENCY_VALUES = ('id', 'kind', 'name', 'description', 'indicators', 'designations', 'grades', 'active')
KIND_ORDER = Case(*(When(kind=k, then=Value(i)) for i, k in enumerate(PerfCompetency.Kind.values)), output_field=IntegerField())


def list_competencies(user: AuthUser, designation: str | None = None, grade: str | None = None, all_: bool = False) -> list[dict]:
    qs = PerfCompetency.objects.for_company(company_id_of(user))
    if not (all_ and has_role(user, 'hr')):
        qs = qs.filter(active=True)
    if designation:
        qs = qs.filter(Q(designations=[]) | Q(designations__contains=[designation]))
    if grade:
        qs = qs.filter(Q(grades=[]) | Q(grades__contains=[grade]))
    return plain_rows(qs.order_by(KIND_ORDER, 'name').values(*COMPETENCY_VALUES))


def get_competency(user: AuthUser, comp_id: str) -> dict:
    _require_hr(user)
    r = PerfCompetency.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(comp_id)).values(*COMPETENCY_VALUES).first()
    if not r:
        raise NotFoundError('competency not found')
    return plain_row(r)


def create_competency(user: AuthUser, body: dict) -> str:
    _require_hr(user)
    b = validate(s.CompetencyIn, body)
    c = PerfCompetency.objects.create(company_id=company_id_of(user), kind=b['kind'], name=b['name'], description=b.get('description'),
                                      indicators=[dict(i) for i in b['indicators']], designations=b['designations'], grades=b['grades'],
                                      active=b['active'])
    return str(c.id)


def update_competency(user: AuthUser, comp_id: str, body: dict) -> None:
    _require_hr(user)
    b = validate(s.CompetencyIn, body, partial=True)
    if 'indicators' in b:
        b['indicators'] = [dict(i) for i in b['indicators']]
    changes = _changes(b, {k: k for k in ('kind', 'name', 'description', 'indicators', 'designations', 'grades', 'active')})
    if changes and not PerfCompetency.objects.for_company(company_id_of(user)).filter(pk=parse_uuid(comp_id)).update(**changes):
        raise NotFoundError('competency not found')


# ---------------------------------------------------------------- cycles

def list_cycles(user: AuthUser) -> list[dict]:
    company_id = company_id_of(user)
    qs = PerfCycle.objects.for_company(company_id)
    if has_role(user, 'hr'):
        mine = Q()
    else:
        e = user.employee_id
        if not e:
            return []                   # not HR and no employee: in no review (filtering on None would match unmanaged ones)
        mine = Q(perf_reviews__manager_employee_id=e)
        qs = qs.exclude(status=Phase.DRAFT).filter(
            Exists(PerfReview.objects.filter(cycle_id=OuterRef('pk')).filter(Q(employee_id=e) | Q(manager_employee_id=e))))
    out = (qs.annotate(participants=Count('perf_reviews', filter=mine),
                       self_done=Count('perf_reviews', filter=mine & Q(perf_reviews__self_submitted_at__isnull=False)),
                       manager_done=Count('perf_reviews', filter=mine & Q(perf_reviews__manager_submitted_at__isnull=False)))
           .order_by('-created_at').values(*CYCLE_VALUES, 'participants', 'self_done', 'manager_done'))
    return plain_rows(out)


def _cycle_fields(b: dict) -> dict:
    out = _changes(b, {'name': 'name', 'periodStart': 'period_start', 'periodEnd': 'period_end', 'goalsWeight': 'goals_weight',
                       'competencyWeight': 'competency_weight', 'ratingLabels': 'rating_labels', 'peerEnabled': 'peer_enabled',
                       'peerMin': 'peer_min', 'selfDue': 'self_due', 'managerDue': 'manager_due', 'peerDue': 'peer_due',
                       'calibrationDue': 'calibration_due', 'filter': 'filter'})
    if 'filter' in out:
        out['filter'] = plain(dict(out['filter']))
    return out


def create_cycle(user: AuthUser, body: dict) -> str:
    _require_hr(user)
    b = validate(s.CycleIn, body)
    if b['periodEnd'] < b['periodStart']:
        raise BadRequestError('the period ends before it starts')
    if b['goalsWeight'] + b['competencyWeight'] != 100:
        raise BadRequestError('goal and competency weights must add up to 100')
    fields = _cycle_fields(b)
    if not fields.get('rating_labels'):
        fields.pop('rating_labels', None)          # the database default: the five standard labels
    c = PerfCycle.objects.create(company_id=company_id_of(user), created_by_id=user.id, **fields)
    return str(c.id)


def update_cycle(user: AuthUser, cycle_id: str, body: dict) -> None:
    _require_hr(user)
    b = validate(s.CycleIn, body, partial=True)
    c = load_cycle(company_id_of(user), parse_uuid(cycle_id))
    always = ('name', 'selfDue', 'managerDue', 'peerDue', 'calibrationDue', 'peerMin')
    if c['status'] != Phase.DRAFT and any(k not in always for k in b):
        raise ConflictError('after launch only the name, deadlines and the peer minimum can change')
    gw = b['goalsWeight'] if 'goalsWeight' in b else num(c['goals_weight'])
    cw = b['competencyWeight'] if 'competencyWeight' in b else num(c['competency_weight'])
    if gw + cw != 100:
        raise BadRequestError('goal and competency weights must add up to 100')
    fields = _cycle_fields(b)
    if fields:
        PerfCycle.objects.filter(pk=c['id']).update(**fields)


def delete_cycle(user: AuthUser, cycle_id: str) -> None:
    _require_hr(user)
    c = load_cycle(company_id_of(user), parse_uuid(cycle_id))
    if c['status'] != Phase.DRAFT:
        raise ConflictError('only a draft cycle can be deleted')
    PerfCycle.objects.filter(pk=c['id']).delete()


def _peer_count(*statuses):
    return Count('perf_peers', filter=Q(perf_peers__status__in=statuses))


def cycle_detail(user: AuthUser, cycle_id: str) -> dict:
    company_id = company_id_of(user)
    c = load_cycle(company_id, parse_uuid(cycle_id))
    hr = has_role(user, 'hr')
    if not hr and c['status'] == Phase.DRAFT:
        raise NotFoundError('review cycle not found')
    if not hr and not user.employee_id:
        raise NotFoundError('review cycle not found')
    qs = PerfReview.objects.filter(cycle_id=c['id'])
    if not hr:
        qs = qs.filter(manager_employee_id=user.employee_id)
    reviews = plain_rows(
        qs.annotate(peers_done=_peer_count(PeerStatus.SUBMITTED), peers_total=_peer_count(PeerStatus.APPROVED, PeerStatus.SUBMITTED),
                    peers_pending=_peer_count(PeerStatus.NOMINATED))
        .order_by('employee__name_en')
        .values('id', 'employee_id', 'manager_employee_id', 'self_submitted_at', 'manager_submitted_at', 'self_score', 'manager_score',
                'final_rating', 'released_at', 'acknowledged_at', 'peers_done', 'peers_total', 'peers_pending',
                emp_code=F('employee__emp_code'), name_en=F('employee__name_en'), designation=F('employee__designation'),
                department=_department(), manager_name=F('manager_employee__name_en')))
    if not hr and not reviews:
        raise NotFoundError('review cycle not found')
    return {'cycle': c, 'reviews': reviews}


def _competency_applies(cp: dict, e: dict) -> bool:
    if cp['designations'] and not (e['designation'] and e['designation'] in cp['designations']):
        return False
    if cp['grades'] and not (e['grade'] and e['grade'] in cp['grades']):
        return False
    return True


def launch_cycle(user: AuthUser, cycle_id: str) -> int:
    """Create one review per participant with their goals for the period and the competencies of their role."""
    _require_hr(user)
    company_id = company_id_of(user)
    with transaction.atomic():
        c = load_cycle(company_id, parse_uuid(cycle_id), lock=True)
        if c['status'] != Phase.DRAFT:
            raise ConflictError('this cycle was already launched')
        f = c['filter'] or {}
        emps = Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE)
        if f.get('orgUnitIds'):
            emps = emps.filter(Exists(current_assignments().filter(employee_id=OuterRef('pk'), org_unit_id__in=f['orgUnitIds'])))
        if f.get('branchIds'):
            emps = emps.filter(Exists(current_assignments().filter(employee_id=OuterRef('pk'), branch_id__in=f['branchIds'])))
        if f.get('joinedBefore'):
            emps = emps.filter(Q(join_date__isnull=True) | Q(join_date__lte=f['joinedBefore']))
        emps = list(emps.values('id', 'designation', 'grade', manager_id=Subquery(
            current_assignments().filter(employee_id=OuterRef('pk')).values('manager_id')[:1])))
        if not emps:
            raise BadRequestError("no active employees match this cycle's filter")
        comps = list(PerfCompetency.objects.for_company(company_id).filter(active=True)
                     .values('id', 'name', 'kind', 'description', 'designations', 'grades'))
        # The participants' individual goals that overlap the period, in one query.
        goals_of: dict[str, list[dict]] = {}
        for g in (PerfGoal.objects.for_company(company_id)
                  .filter(employee_id__in=[e['id'] for e in emps], scope=PerfGoal.Scope.INDIVIDUAL,
                          status__in=(PerfGoal.Status.ACTIVE, PerfGoal.Status.COMPLETED))
                  .filter(Q(due_date__isnull=True) | Q(due_date__gte=c['period_start']))
                  .filter(Q(start_date__isnull=True) | Q(start_date__lte=c['period_end']))
                  .order_by('created_at', 'id').values('employee_id', 'id', 'title', 'description', 'weight')):
            goals_of.setdefault(str(g['employee_id']), []).append(g)
        for e in emps:
            rv = PerfReview.objects.create(cycle_id=c['id'], company_id=company_id, employee_id=e['id'], manager_employee_id=e['manager_id'])
            rid = str(rv.id)
            items = [PerfReviewItem(review_id=rid, item_type=ItemType.GOAL, ref_id=g['id'], title=g['title'], detail=g['description'],
                                    weight=g['weight'] if g['weight'] > 0 else 1) for g in goals_of.get(str(e['id']), [])]
            for cp in comps:
                if not _competency_applies(cp, e):
                    continue
                kind = cp['kind'][:1].upper() + cp['kind'][1:]
                items.append(PerfReviewItem(review_id=rid, item_type=ItemType.COMPETENCY, ref_id=cp['id'], title=cp['name'],
                                            detail=f"{kind}{': ' + cp['description'] if cp['description'] else ''}", weight=1))
            PerfReviewItem.objects.bulk_create(items)
            emp = str(e['id'])
            notify(company_id=company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='review.self_open',
                   title=f"{c['name']}: your self review is open",
                   body=f"Please complete it by {bs_label(c['self_due'])}." if c['self_due'] else 'Rate your goals and competencies and add a summary.',
                   payload={'reviewId': rid})
        PerfCycle.objects.filter(pk=c['id']).update(status=Phase.SELF_REVIEW, launched_at=Now())
        audit('launch', 'perf_cycle', actor_id=user.id, row_id=c['id'], after={'participants': len(emps)})
    return len(emps)


def next_phase(c: dict) -> str:
    i = PHASES.index(c['status']) + 1
    if i < len(PHASES) and PHASES[i] == Phase.PEER_REVIEW and not c['peer_enabled']:
        i += 1
    return PHASES[min(i, len(PHASES) - 1)]


def advance_cycle(user: AuthUser, cycle_id: str) -> str:
    """Move the cycle to its next phase. Releasing fixes final ratings, shows results and turns training needs into nominations."""
    _require_hr(user)
    from hajiri.learning.services import create_review_nominations
    company_id = company_id_of(user)
    with transaction.atomic():
        c = load_cycle(company_id, parse_uuid(cycle_id), lock=True)
        if c['status'] == Phase.DRAFT:
            raise ConflictError('launch the cycle first')
        if c['status'] == Phase.CLOSED:
            raise ConflictError('this cycle is closed')
        to = next_phase(c)
        PerfCycle.objects.filter(pk=c['id']).update(status=to)
        reviews = PerfReview.objects.filter(cycle_id=c['id'])
        if to == Phase.MANAGER_REVIEW:
            managers = (reviews.filter(manager_employee_id__isnull=False, manager_submitted_at__isnull=True)
                        .values_list('manager_employee_id', flat=True).distinct())
            for m in managers:
                mid = str(m)
                notify(company_id=company_id, user_id=user_id_for_employee(mid), employee_id=mid, template='review.manager_open',
                       title=f"{c['name']}: manager reviews are open",
                       body=f"Review your team by {bs_label(c['manager_due'])}." if c['manager_due'] else 'Review your team.',
                       payload={'cycleId': c['id']})
        if to == Phase.PEER_REVIEW:
            peers = (PerfPeer.objects.filter(review__cycle_id=c['id'], status=PeerStatus.APPROVED)
                     .values_list('reviewer_employee_id', flat=True).distinct())
            for p in peers:
                pid = str(p)
                notify(company_id=company_id, user_id=user_id_for_employee(pid), employee_id=pid, template='review.peer_open',
                       title=f"{c['name']}: peer feedback requested", body='Colleagues asked for your feedback. Open Growth, Reviews to respond.',
                       payload={'cycleId': c['id']})
        if to == Phase.CALIBRATION:
            notify_role(company_id, 'hr', template='review.calibration', title=f"{c['name']}: ready for calibration",
                        body='Compare ratings across teams and set final ratings.', payload={'cycleId': c['id']})
        if to == Phase.RELEASED:
            reviews.update(final_rating=Coalesce('final_rating', 'manager_score'), released_at=Now())
            PerfCycle.objects.filter(pk=c['id']).update(released_at=Now())
            for rv in reviews.values('id', 'employee_id', 'recommendation'):
                rec = rv['recommendation'] or {}
                emp = str(rv['employee_id'])
                if rec.get('trainingCourseIds'):
                    create_review_nominations(company_id, emp, str(rv['id']), rec['trainingCourseIds'], user.id)
                notify(company_id=company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='review.released',
                       title=f"{c['name']}: your review is ready", body="Read your manager's review and acknowledge it.",
                       payload={'reviewId': str(rv['id'])})
        audit('advance', 'perf_cycle', actor_id=user.id, row_id=c['id'], before={'status': c['status']}, after={'status': to})
    return to


def calibration(user: AuthUser, cycle_id: str) -> dict:
    _require_hr(user)
    c = load_cycle(company_id_of(user), parse_uuid(cycle_id))
    rs = plain_rows(PerfReview.objects.filter(cycle_id=c['id'])
                    .annotate(department=Coalesce(_department(), Value('No department'), output_field=TextField()))
                    .order_by('department', 'employee__name_en')
                    .values('id', 'employee_id', 'department', 'self_score', 'manager_score', 'final_rating', 'calibration_reason',
                            'recommendation', name_en=F('employee__name_en'), emp_code=F('employee__emp_code')))
    distribution: dict[str, list[int]] = {}
    overall = [0, 0, 0, 0, 0]
    for r in rs:
        v = r['final_rating'] if r['final_rating'] is not None else r['manager_score']
        if v is None:
            continue
        b = min(5, max(1, round_half_up(num(v))))
        distribution.setdefault(r['department'] or 'No department', [0, 0, 0, 0, 0])[b - 1] += 1
        overall[b - 1] += 1
    return {'cycle': c, 'reviews': rs, 'distribution': {'overall': overall, 'byDepartment': distribution}}


# ---------------------------------------------------------------- reviews

def list_reviews(user: AuthUser, scope: str = 'mine') -> list[dict]:
    scope = validate(s.ReviewScopeIn, {'scope': scope or 'mine'})['scope']
    if not user.employee_id:
        return []
    qs = PerfReview.objects.for_company(company_id_of(user)).exclude(cycle__status=Phase.DRAFT)
    qs = qs.filter(employee_id=user.employee_id) if scope == 'mine' else qs.filter(manager_employee_id=user.employee_id)
    out = []
    for r in plain_rows(qs.order_by('-cycle__created_at', 'employee__name_en').values(
            'id', 'cycle_id', 'employee_id', 'self_submitted_at', 'manager_submitted_at', 'released_at', 'acknowledged_at', 'final_rating',
            'manager_score', 'self_score', cycle_name=F('cycle__name'), cycle_status=F('cycle__status'), period_start=F('cycle__period_start'),
            period_end=F('cycle__period_end'), self_due=F('cycle__self_due'), manager_due=F('cycle__manager_due'),
            rating_labels=F('cycle__rating_labels'), name_en=F('employee__name_en'), emp_code=F('employee__emp_code'),
            designation=F('employee__designation'))):
        # The employee sees the final rating once released, and never the manager's score.
        if scope != 'team' and not r['released_at']:
            r['final_rating'] = None
        if scope != 'team':
            r['manager_score'] = None
        out.append(r)
    return out


ITEM_VALUES = ('id', 'item_type', 'ref_id', 'title', 'detail', 'weight', 'self_rating', 'self_comment', 'manager_rating', 'manager_comment')


def _items(review_id: str) -> list[dict]:
    return plain_rows(PerfReviewItem.objects.filter(review_id=review_id).order_by('-item_type', 'title').values(*ITEM_VALUES))


def review_detail(user: AuthUser, review_id: str) -> dict:
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(review_id))
    role = viewer_role(user, rv)
    if not role or rv['cycle']['status'] == Phase.DRAFT:
        raise NotFoundError('review not found')
    row = plain_row(PerfReview.objects.filter(pk=rv['id']).values(
        *REVIEW_VALUES, name_en=F('employee__name_en'), emp_code=F('employee__emp_code'), designation=F('employee__designation'),
        manager_name=F('manager_employee__name_en')).get())
    items = _items(rv['id'])
    peers = plain_rows(PerfPeer.objects.filter(review_id=rv['id']).order_by('reviewer_employee__name_en')
                       .values('id', 'reviewer_employee_id', 'status', 'rating', 'strengths', 'improvements', 'submitted_at',
                               reviewer_name=F('reviewer_employee__name_en')))
    released = bool(rv['released_at'])
    phase = rv['cycle']['status']
    if role == 'self':
        show = released
        return {
            'role': role, 'cycle': rv['cycle'],
            'review': {k: row[k] for k in ('id', 'employee_id', 'name_en', 'emp_code', 'designation', 'manager_name', 'self_summary',
                                            'self_submitted_at', 'self_score', 'manager_submitted_at', 'released_at', 'acknowledged_at',
                                            'employee_comment')}
            | {'manager_summary': row['manager_summary'] if show else None, 'final_rating': row['final_rating'] if show else None},
            'items': [i if show else {**i, 'manager_rating': None, 'manager_comment': None} for i in items],
            'peers': {'nominations': [{'id': p['id'], 'reviewer_name': p['reviewer_name'],
                                       'status': PeerStatus.APPROVED if p['status'] == PeerStatus.SUBMITTED else p['status']} for p in peers],
                      'aggregate': peer_aggregate(peers, rv['cycle']['peer_min']) if released else None},
            'journal': [],
            'can': {'editSelf': not rv['self_submitted_at'] and phase in (Phase.SELF_REVIEW, Phase.MANAGER_REVIEW),
                    'nominatePeers': rv['cycle']['peer_enabled'] and phase in OPEN_PHASES,
                    'acknowledge': released and not rv['acknowledged_at'], 'editManager': False, 'calibrate': False},
        }
    # Manager's journal: a manager sees their own notes for the period; HR sees everyone's.
    notes = PerfJournal.objects.for_company(company_id).filter(
        employee_id=rv['employee_id'], note_date__range=(rv['cycle']['period_start'], rv['cycle']['period_end']))
    if role != 'hr':
        notes = notes.filter(author_id=user.id)
    journal = plain_rows(notes.order_by('note_date').values('id', 'note_date', 'tag', 'note', author_name=F('author__name')))
    manager_open = phase in OPEN_PHASES and not rv['manager_submitted_at'] and (bool(rv['self_submitted_at']) or phase != Phase.SELF_REVIEW)
    return {
        'role': role, 'cycle': rv['cycle'], 'review': row, 'items': items,
        'peers': {'nominations': peers, 'aggregate': peer_aggregate(peers, 1)},
        'journal': journal,
        'can': {'editSelf': False,
                'editManager': manager_open and (role == 'hr' or rv['manager_employee_id'] == user.employee_id or not rv['manager_employee_id']),
                'nominatePeers': rv['cycle']['peer_enabled'] and phase in OPEN_PHASES,
                'calibrate': role == 'hr' and phase == Phase.CALIBRATION, 'acknowledge': False},
    }


def _save_ratings(review_id: str, side: str, ratings: list[dict]) -> None:
    for it in ratings:
        if not PerfReviewItem.objects.filter(pk=it['id'], review_id=review_id).update(
                **{f'{side}_rating': it['rating'], f'{side}_comment': it.get('comment')}):
            raise BadRequestError('an item does not belong to this review')


def _score_of(review_id: str, side: str, c: dict, require_all: bool) -> float | None:
    items = list(PerfReviewItem.objects.filter(review_id=review_id).values('item_type', 'weight', rating=F(f'{side}_rating')))
    if require_all and any(i['rating'] is None for i in items):
        raise BadRequestError('rate every goal and competency before submitting')
    return overall_score(items, num(c['goals_weight']), num(c['competency_weight']))


def save_self(user: AuthUser, review_id: str, body: dict) -> None:
    b = validate(s.SelfReviewIn, body)
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(review_id))
    if not user.employee_id or rv['employee_id'] != user.employee_id:
        raise NotFoundError('review not found')
    if rv['cycle']['status'] not in (Phase.SELF_REVIEW, Phase.MANAGER_REVIEW):
        raise ConflictError('the self review is closed')
    if rv['self_submitted_at']:
        raise ConflictError('you already submitted your self review')
    review = PerfReview.objects.filter(pk=rv['id'])
    with transaction.atomic():
        _save_ratings(rv['id'], 'self', b['items'])
        if 'summary' in b:
            review.update(self_summary=b['summary'])
        if b['submit']:
            score = _score_of(rv['id'], 'self', rv['cycle'], True)
            review.update(self_submitted_at=Now(), self_score=score)
            if rv['manager_employee_id']:
                m = rv['manager_employee_id']
                notify(company_id=company_id, user_id=user_id_for_employee(m), employee_id=m, template='review.self_submitted',
                       title='Self review submitted', body=f"{user.name} submitted their self review for {rv['cycle']['name']}.",
                       payload={'reviewId': rv['id']})


def save_manager(user: AuthUser, review_id: str, body: dict) -> None:
    b = validate(s.ManagerReviewIn, body)
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(review_id))
    role = viewer_role(user, rv)
    if not role or role == 'self':
        raise NotFoundError('review not found')
    if role == 'manager' and rv['manager_employee_id'] and rv['manager_employee_id'] != user.employee_id:
        raise ForbiddenError('the direct manager writes this review')
    phase = rv['cycle']['status']
    if phase not in OPEN_PHASES:
        raise ConflictError('manager reviews are closed')
    if phase == Phase.SELF_REVIEW and not rv['self_submitted_at']:
        raise ConflictError('wait for the self review, or until the manager review phase starts')
    if rv['manager_submitted_at']:
        raise ConflictError('the manager review was already submitted')
    rec = plain(dict(b['recommendation'])) if b.get('recommendation') is not None else None
    if rec and rec['trainingCourseIds']:
        wanted = set(rec['trainingCourseIds'])
        if LrnCourse.objects.for_company(company_id).filter(pk__in=wanted).count() != len(wanted):
            raise BadRequestError('a recommended course was not found')
    review = PerfReview.objects.filter(pk=rv['id'])
    with transaction.atomic():
        _save_ratings(rv['id'], 'manager', b['items'])
        if 'summary' in b:
            review.update(manager_summary=b['summary'])
        if rec is not None:
            review.update(recommendation=rec)
        if b['submit']:
            score = _score_of(rv['id'], 'manager', rv['cycle'], True)
            review.update(manager_submitted_at=Now(), manager_score=score)
            audit('submit', 'perf_review', actor_id=user.id, row_id=rv['id'], after={'managerScore': score})


def calibrate(user: AuthUser, review_id: str, body: dict) -> None:
    _require_hr(user)
    b = validate(s.CalibrateIn, body)
    rv = load_review(company_id_of(user), parse_uuid(review_id))
    if rv['cycle']['status'] != Phase.CALIBRATION:
        raise ConflictError('final ratings are set during calibration')
    fr = round2(b['finalRating'])
    with transaction.atomic():
        PerfReview.objects.filter(pk=rv['id']).update(final_rating=fr, calibrated_by_id=user.id, calibration_reason=b['reason'])
        audit('calibrate', 'perf_review', actor_id=user.id, row_id=rv['id'],
              before={'finalRating': None if rv['final_rating'] is None else str(rv['final_rating']),
                      'managerScore': None if rv['manager_score'] is None else str(rv['manager_score'])},
              after={'finalRating': fr, 'reason': b['reason']})


def acknowledge(user: AuthUser, review_id: str, body: dict | None = None) -> None:
    b = validate(s.AcknowledgeIn, body or {})
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(review_id))
    if not user.employee_id or rv['employee_id'] != user.employee_id:
        raise NotFoundError('review not found')
    if not rv['released_at']:
        raise ConflictError('the review has not been released yet')
    if rv['acknowledged_at']:
        raise ConflictError('already acknowledged')
    with transaction.atomic():
        PerfReview.objects.filter(pk=rv['id']).update(acknowledged_at=Now(), employee_comment=b.get('comment'))
        if rv['manager_employee_id']:
            m = rv['manager_employee_id']
            notify(company_id=company_id, user_id=user_id_for_employee(m), employee_id=m, template='review.acknowledged',
                   title='Review acknowledged',
                   body=f"{user.name} acknowledged their {rv['cycle']['name']} review{' with a comment' if b.get('comment') else ''}.",
                   payload={'reviewId': rv['id']})


# ---------------------------------------------------------------- peers (360)

def nominate_peers(user: AuthUser, review_id: str, body: dict) -> int:
    b = plain(validate(s.NominatePeersIn, body))
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(review_id))
    role = viewer_role(user, rv)
    if not role:
        raise NotFoundError('review not found')
    if not rv['cycle']['peer_enabled']:
        raise ConflictError('this cycle has no peer reviews')
    if rv['cycle']['status'] not in OPEN_PHASES:
        raise ConflictError('peer nominations are closed')
    status = PeerStatus.NOMINATED if role == 'self' else PeerStatus.APPROVED
    added = 0
    active = Employee.objects.for_company(company_id).filter(status=Employee.Status.ACTIVE)
    with transaction.atomic():
        for eid in dict.fromkeys(b['employeeIds']):
            if eid == rv['employee_id']:
                raise BadRequestError('you cannot review yourself')
            if not active.filter(pk=eid).exists():
                raise NotFoundError('colleague not found')
            _, created = PerfPeer.objects.get_or_create(review_id=rv['id'], reviewer_employee_id=eid,
                                                        defaults={'status': status, 'nominated_by_id': user.id})
            added += created
        if status == PeerStatus.NOMINATED and added and rv['manager_employee_id']:
            m = rv['manager_employee_id']
            notify(company_id=company_id, user_id=user_id_for_employee(m), employee_id=m, template='review.peers_nominated',
                   title='Peer reviewers to approve',
                   body=f"{user.name} nominated {added} peer reviewer{'' if added == 1 else 's'} for {rv['cycle']['name']}.",
                   payload={'reviewId': rv['id']})
    return added


def _load_peer(company_id: str, peer_id: str) -> dict:
    r = PerfPeer.objects.for_company(company_id).filter(pk=peer_id).values('id', 'review_id', 'reviewer_employee_id', 'status').first()
    if not r:
        raise NotFoundError('peer review not found')
    return plain_row(r)


def decide_peer(user: AuthUser, peer_id: str, body: dict) -> dict:
    b = validate(s.DecidePeerIn, body)
    company_id = company_id_of(user)
    p = _load_peer(company_id, parse_uuid(peer_id))
    rv = load_review(company_id, p['review_id'])
    role = viewer_role(user, rv)
    if role not in ('manager', 'hr'):
        raise ForbiddenError('the manager approves peer reviewers')
    if p['status'] != PeerStatus.NOMINATED:
        raise ConflictError(f"already {p['status']}")
    with transaction.atomic():
        PerfPeer.objects.filter(pk=p['id']).update(status=PeerStatus.APPROVED if b['approve'] else PeerStatus.DECLINED)
        if b['approve'] and rv['cycle']['status'] in (Phase.MANAGER_REVIEW, Phase.PEER_REVIEW, Phase.SELF_REVIEW):
            r = p['reviewer_employee_id']
            notify(company_id=company_id, user_id=user_id_for_employee(r), employee_id=r, template='review.peer_requested',
                   title='Peer feedback requested', body=f"You were asked to give feedback in {rv['cycle']['name']}. Open Growth, Reviews.",
                   payload={'peerId': p['id']})
    return rv


def remove_peer(user: AuthUser, peer_id: str) -> dict:
    company_id = company_id_of(user)
    p = _load_peer(company_id, parse_uuid(peer_id))
    rv = load_review(company_id, p['review_id'])
    role = viewer_role(user, rv)
    if not role:
        raise NotFoundError('peer review not found')
    if p['status'] == PeerStatus.SUBMITTED:
        raise ConflictError('this colleague already responded')
    if role == 'self' and p['status'] != PeerStatus.NOMINATED:
        raise ForbiddenError('ask your manager to remove an approved reviewer')
    PerfPeer.objects.filter(pk=p['id']).delete()
    return rv


def peer_requests(user: AuthUser) -> list[dict]:
    """Peer feedback I have been asked to give."""
    if not user.employee_id:
        return []
    return plain_rows(
        PerfPeer.objects.for_company(company_id_of(user))
        .filter(reviewer_employee_id=user.employee_id, status__in=(PeerStatus.APPROVED, PeerStatus.SUBMITTED))
        .exclude(review__cycle__status__in=(Phase.DRAFT, Phase.CLOSED))
        .order_by('status', '-review__cycle__created_at')
        .values('id', 'status', 'rating', 'strengths', 'improvements', 'submitted_at', employee_name=F('review__employee__name_en'),
                designation=F('review__employee__designation'), cycle_name=F('review__cycle__name'),
                cycle_status=F('review__cycle__status'), peer_due=F('review__cycle__peer_due'),
                rating_labels=F('review__cycle__rating_labels')))


def submit_peer(user: AuthUser, peer_id: str, body: dict) -> None:
    b = validate(s.PeerResponseIn, body)
    company_id = company_id_of(user)
    p = _load_peer(company_id, parse_uuid(peer_id))
    if not user.employee_id or p['reviewer_employee_id'] != user.employee_id:
        raise NotFoundError('peer review not found')
    if p['status'] != PeerStatus.APPROVED:
        raise ConflictError('you already responded' if p['status'] == PeerStatus.SUBMITTED else 'this request is not open')
    rv = load_review(company_id, p['review_id'])
    if rv['cycle']['status'] not in OPEN_PHASES:
        raise ConflictError('peer feedback is closed')
    PerfPeer.objects.filter(pk=p['id']).update(rating=b['rating'], strengths=b.get('strengths'), improvements=b.get('improvements'),
                                               status=PeerStatus.SUBMITTED, submitted_at=Now())


# ---------------------------------------------------------------- printable summary

def review_print_html(user: AuthUser, review_id: str) -> str:
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(review_id))
    role = viewer_role(user, rv)
    if not role or rv['cycle']['status'] == Phase.DRAFT or (role == 'self' and not rv['released_at']):
        raise NotFoundError('review not found')
    h = PerfReview.objects.filter(pk=rv['id']).values(
        'self_summary', 'manager_summary', 'final_rating', 'manager_score', 'self_score', 'recommendation', 'employee_comment',
        'acknowledged_at', 'calibration_reason', name_en=F('employee__name_en'), emp_code=F('employee__emp_code'),
        designation=F('employee__designation'), manager_name=F('manager_employee__name_en'), company_name=F('company__name_en')).get()
    cyc = rv['cycle']
    labels = cyc['rating_labels'] or []

    def rated(n) -> str:
        """'4 Exceeds expectations', '3.60 Meets expectations' or a dash."""
        if n is None:
            return '—'
        v = num(n)
        idx = min(5, max(1, round_half_up(v))) - 1
        return f"{f'{v:.0f}' if v.is_integer() else f'{v:.2f}'} {labels[idx] if idx < len(labels) else ''}"

    items = [{**i, 'self_label': rated(i['self_rating']), 'manager_label': rated(i['manager_rating']), 'weight_text': fmt_number(i['weight'])}
             for i in PerfReviewItem.objects.filter(review_id=rv['id']).order_by('-item_type', 'title')
             .values('item_type', 'title', 'weight', 'self_rating', 'self_comment', 'manager_rating', 'manager_comment')]
    rec = None if role == 'self' else h['recommendation']
    recommendation = None
    if rec is not None:
        parts = [f"Increment {fmt_number(rec['incrementPct'])}%" if rec.get('incrementPct') is not None else None,
                 'Promotion' if rec.get('promotion') else None, 'Performance improvement plan' if rec.get('pip') else None,
                 rec.get('notes') or None]
        recommendation = '\n'.join(p for p in parts if p) or '—'
    final = h['final_rating'] if h['final_rating'] is not None else h['manager_score']
    return render_to_string('performance/review_print.html', {
        'h': h, 'cycle': cyc, 'period': f"{bs_label(cyc['period_start'])} to {bs_label(cyc['period_end'])}",
        'self_score': rated(h['self_score']), 'final': rated(final),
        'goals_weight': fmt_number(cyc['goals_weight']), 'competency_weight': fmt_number(cyc['competency_weight']),
        'goals': [i for i in items if i['item_type'] == ItemType.GOAL],
        'competencies': [i for i in items if i['item_type'] == ItemType.COMPETENCY],
        'recommendation': recommendation, 'calibration_note': h['calibration_reason'] if role == 'hr' else None,
        'acknowledged_on': bs_label(h['acknowledged_at']) if h['acknowledged_at'] else None})
