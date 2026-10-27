"""
Competency framework and review cycles: self, manager and peer (360) reviews, calibration, release and
acknowledgement, and the printable summary (port of apps/api/src/modules/performance/reviews.ts).
"""
from __future__ import annotations

import json

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import Arr, Bool, Enum, Int, IsoDate, Num, Obj, Str, Uuid, parse, parse_uuid
from hajiri.notifications.service import notify, notify_role, user_id_for_employee

from .common import esc, fmt_number, is_above_in_chain, js_round, num, overall_score, round2

PHASES = ('draft', 'self_review', 'manager_review', 'peer_review', 'calibration', 'released', 'closed')
PHASE_LABEL = {'draft': 'Draft', 'self_review': 'Self review', 'manager_review': 'Manager review', 'peer_review': 'Peer review',
               'calibration': 'Calibration', 'released': 'Released', 'closed': 'Closed'}
DEFAULT_LABELS = ['Needs improvement', 'Partly meets', 'Meets expectations', 'Exceeds expectations', 'Outstanding']
OPEN_PHASES = ('self_review', 'manager_review', 'peer_review')


def _locale_key(s: str):
    # localeCompare for plain text: case-insensitive first, lowercase before uppercase on ties.
    return (s.casefold(), s.swapcase())


def peer_aggregate(peers: list[dict], minimum: int) -> dict:
    """Peer responses are shown to the reviewed employee only as an anonymous aggregate of at least `minimum` responses."""
    done = [p for p in peers if p['status'] == 'submitted']
    if len(done) < minimum:
        return {'count': len(done), 'shown': False}
    rated = [p for p in done if p['rating'] is not None]

    def sort_text(xs):
        return sorted([x for x in xs if x and x.strip()], key=_locale_key)
    return {
        'count': len(done), 'shown': True,
        'averageRating': round2(sum(p['rating'] for p in rated) / len(rated)) if rated else None,
        'strengths': sort_text(p['strengths'] for p in done), 'improvements': sort_text(p['improvements'] for p in done),
    }


def _ids(r: dict, *keys: str) -> dict:
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


def load_cycle(company_id: str, cycle_id: str, lock: bool = False) -> dict:
    r = one(f"SELECT * FROM perf_cycle WHERE id = %s AND company_id = %s{' FOR UPDATE' if lock else ''}", [cycle_id, company_id])
    if not r:
        raise NotFoundError('review cycle not found')
    return _ids(r, 'id', 'company_id', 'created_by')


def load_review(company_id: str, review_id: str) -> dict:
    rv = one('SELECT * FROM perf_review WHERE id = %s AND company_id = %s', [review_id, company_id])
    if not rv:
        raise NotFoundError('review not found')
    _ids(rv, 'id', 'cycle_id', 'company_id', 'employee_id', 'manager_employee_id', 'calibrated_by')
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


def _cycle_fields(partial: bool = False) -> dict:
    o = {'optional': True} if partial else {}
    return {
        'name': Str(trim=True, min=1, max=120, **o),
        'periodStart': IsoDate(**o),
        'periodEnd': IsoDate(**o),
        'goalsWeight': Num(min=0, max=100, coerce=True, **(o or {'default': 60})),
        'competencyWeight': Num(min=0, max=100, coerce=True, **(o or {'default': 40})),
        'ratingLabels': Arr(Str(trim=True, min=1, max=40), min=5, max=5, optional=True),
        'peerEnabled': Bool(**(o or {'default': False})),
        'peerMin': Int(min=1, max=10, coerce=True, **(o or {'default': 3})),
        'selfDue': IsoDate(nullish=True),
        'managerDue': IsoDate(nullish=True),
        'peerDue': IsoDate(nullish=True),
        'calibrationDue': IsoDate(nullish=True),
        'filter': Obj({'orgUnitIds': Arr(Uuid(), max=200, optional=True), 'branchIds': Arr(Uuid(), max=200, optional=True),
                       'joinedBefore': IsoDate(optional=True)}, **(o or {'default': dict})),
    }


CYCLE_BODY = Obj(_cycle_fields())
# PATCH changes only what is sent (zod's partial() also filled in defaults, so after launch every edit was refused).
CYCLE_PATCH = Obj(_cycle_fields(partial=True))


def _competency_fields(partial: bool = False) -> dict:
    o = {'optional': True} if partial else {}
    return {
        'kind': Enum(('knowledge', 'skill', 'attitude'), **o),
        'name': Str(trim=True, min=1, max=120, **o),
        'description': Str(trim=True, max=2000, nullish=True),
        'indicators': Arr(Obj({'level': Int(min=1, max=5), 'text': Str(trim=True, min=1, max=500)}), max=5, **(o or {'default': list})),
        'designations': Arr(Str(trim=True, min=1, max=120), max=100, **(o or {'default': list})),
        'grades': Arr(Str(trim=True, min=1, max=40), max=50, **(o or {'default': list})),
        'active': Bool(**(o or {'default': True})),
    }


COMPETENCY_BODY = Obj(_competency_fields())
COMPETENCY_PATCH = Obj(_competency_fields(partial=True))

RECOMMENDATION = Obj({
    'incrementPct': Num(min=0, max=100, coerce=True, nullish=True),
    'promotion': Bool(default=False),
    'pip': Bool(default=False),
    'trainingCourseIds': Arr(Uuid(), max=10, default=list),
    'notes': Str(trim=True, max=2000, nullish=True),
})
ITEM_RATINGS = Arr(Obj({'id': Uuid(), 'rating': Int(min=1, max=5, nullable=True), 'comment': Str(trim=True, max=2000, nullish=True)}), max=100)


def _require_hr(user: AuthUser) -> None:
    if not has_role(user, 'hr'):
        raise ForbiddenError('requires role hr')


# ---------------------------------------------------------------- competency framework

def list_competencies(user: AuthUser, designation: str | None = None, grade: str | None = None, all_: bool = False) -> list[dict]:
    show_inactive = bool(all_) and has_role(user, 'hr')
    out = rows("""SELECT id, kind, name, description, indicators, designations, grades, active FROM perf_competency
                   WHERE company_id = %s AND (active OR %s)
                     AND (%s::text IS NULL OR cardinality(designations) = 0 OR %s = ANY(designations))
                     AND (%s::text IS NULL OR cardinality(grades) = 0 OR %s = ANY(grades))
                   ORDER BY array_position(ARRAY['knowledge','skill','attitude'], kind), name""",
               [company_id_of(user), show_inactive, designation or None, designation or None, grade or None, grade or None])
    for c in out:
        c['id'] = str(c['id'])
    return out


def get_competency(user: AuthUser, comp_id: str) -> dict:
    _require_hr(user)
    r = one('SELECT id, kind, name, description, indicators, designations, grades, active FROM perf_competency WHERE id = %s AND company_id = %s',
            [parse_uuid(str(comp_id)), company_id_of(user)])
    if not r:
        raise NotFoundError('competency not found')
    r['id'] = str(r['id'])
    return r


def create_competency(user: AuthUser, body: dict) -> str:
    _require_hr(user)
    b = parse(COMPETENCY_BODY, body)
    return str(one("""INSERT INTO perf_competency (company_id, kind, name, description, indicators, designations, grades, active)
                      VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
                   [company_id_of(user), b['kind'], b['name'], b.get('description'), json.dumps(b['indicators']), b['designations'],
                    b['grades'], b['active']])['id'])


def update_competency(user: AuthUser, comp_id: str, body: dict) -> None:
    _require_hr(user)
    b = parse(COMPETENCY_PATCH, body)
    sets, vals = [], []
    for k, col in {'kind': 'kind', 'name': 'name', 'description': 'description', 'indicators': 'indicators', 'designations': 'designations',
                   'grades': 'grades', 'active': 'active'}.items():
        if k in b:
            vals.append(json.dumps(b[k]) if k == 'indicators' else b[k])
            sets.append(f'{col} = %s')
    if not sets:
        return
    if not execute(f"UPDATE perf_competency SET {', '.join(sets)} WHERE id = %s AND company_id = %s",
                   [*vals, parse_uuid(str(comp_id)), company_id_of(user)]):
        raise NotFoundError('competency not found')


# ---------------------------------------------------------------- cycles

def list_cycles(user: AuthUser) -> list[dict]:
    company_id = company_id_of(user)
    if has_role(user, 'hr'):
        out = rows("""SELECT c.*, (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id) AS participants,
                             (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.self_submitted_at IS NOT NULL) AS self_done,
                             (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.manager_submitted_at IS NOT NULL) AS manager_done
                        FROM perf_cycle c WHERE c.company_id = %s ORDER BY c.created_at DESC""", [company_id])
    else:
        e = user.employee_id
        out = rows("""SELECT c.*, (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.manager_employee_id = %s) AS participants,
                             (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.manager_employee_id = %s AND r.self_submitted_at IS NOT NULL) AS self_done,
                             (SELECT count(*)::int FROM perf_review r WHERE r.cycle_id = c.id AND r.manager_employee_id = %s AND r.manager_submitted_at IS NOT NULL) AS manager_done
                        FROM perf_cycle c WHERE c.company_id = %s AND c.status <> 'draft'
                         AND EXISTS (SELECT 1 FROM perf_review r WHERE r.cycle_id = c.id AND (r.employee_id = %s OR r.manager_employee_id = %s))
                       ORDER BY c.created_at DESC""", [e, e, e, company_id, e, e])
    return [_ids(c, 'id', 'company_id', 'created_by') for c in out]


def create_cycle(user: AuthUser, body: dict) -> str:
    _require_hr(user)
    b = parse(CYCLE_BODY, body)
    if b['periodEnd'] < b['periodStart']:
        raise BadRequestError('the period ends before it starts')
    if b['goalsWeight'] + b['competencyWeight'] != 100:
        raise BadRequestError('goal and competency weights must add up to 100')
    return str(one("""
        INSERT INTO perf_cycle (company_id, name, period_start, period_end, goals_weight, competency_weight, rating_labels, peer_enabled, peer_min,
                                self_due, manager_due, peer_due, calibration_due, filter, created_by)
        VALUES (%s, %s, %s, %s, %s, %s, coalesce(%s::jsonb, '["Needs improvement", "Partly meets", "Meets expectations", "Exceeds expectations", "Outstanding"]'::jsonb),
                %s, %s, %s, %s, %s, %s, %s, %s) RETURNING id""",
        [company_id_of(user), b['name'], b['periodStart'], b['periodEnd'], b['goalsWeight'], b['competencyWeight'],
         json.dumps(b['ratingLabels']) if b.get('ratingLabels') else None, b['peerEnabled'], b['peerMin'], b.get('selfDue'),
         b.get('managerDue'), b.get('peerDue'), b.get('calibrationDue'), json.dumps(b['filter']), user.id])['id'])


def update_cycle(user: AuthUser, cycle_id: str, body: dict) -> None:
    _require_hr(user)
    b = parse(CYCLE_PATCH, body)
    c = load_cycle(company_id_of(user), parse_uuid(str(cycle_id)))
    always = ('name', 'selfDue', 'managerDue', 'peerDue', 'calibrationDue', 'peerMin')
    if c['status'] != 'draft' and any(k not in always for k in b):
        raise ConflictError('after launch only the name, deadlines and the peer minimum can change')
    gw = b['goalsWeight'] if 'goalsWeight' in b else num(c['goals_weight'])
    cw = b['competencyWeight'] if 'competencyWeight' in b else num(c['competency_weight'])
    if gw + cw != 100:
        raise BadRequestError('goal and competency weights must add up to 100')
    mapping = {'name': 'name', 'periodStart': 'period_start', 'periodEnd': 'period_end', 'goalsWeight': 'goals_weight',
               'competencyWeight': 'competency_weight', 'ratingLabels': 'rating_labels', 'peerEnabled': 'peer_enabled', 'peerMin': 'peer_min',
               'selfDue': 'self_due', 'managerDue': 'manager_due', 'peerDue': 'peer_due', 'calibrationDue': 'calibration_due', 'filter': 'filter'}
    sets, vals = [], []
    for k, col in mapping.items():
        if k in b:
            vals.append(json.dumps(b[k]) if k in ('ratingLabels', 'filter') else b[k])
            sets.append(f'{col} = %s')
    if sets:
        execute(f"UPDATE perf_cycle SET {', '.join(sets)} WHERE id = %s", [*vals, c['id']])


def delete_cycle(user: AuthUser, cycle_id: str) -> None:
    _require_hr(user)
    c = load_cycle(company_id_of(user), parse_uuid(str(cycle_id)))
    if c['status'] != 'draft':
        raise ConflictError('only a draft cycle can be deleted')
    execute('DELETE FROM perf_cycle WHERE id = %s', [c['id']])


def cycle_detail(user: AuthUser, cycle_id: str) -> dict:
    company_id = company_id_of(user)
    c = load_cycle(company_id, parse_uuid(str(cycle_id)))
    hr = has_role(user, 'hr')
    if not hr and c['status'] == 'draft':
        raise NotFoundError('review cycle not found')
    reviews = rows("""
        SELECT r.id, r.employee_id, e.emp_code, e.name_en, e.designation, ou.name_en AS department, m.name_en AS manager_name, r.manager_employee_id,
               r.self_submitted_at, r.manager_submitted_at, r.self_score, r.manager_score, r.final_rating, r.released_at, r.acknowledged_at,
               (SELECT count(*)::int FROM perf_peer p WHERE p.review_id = r.id AND p.status = 'submitted') AS peers_done,
               (SELECT count(*)::int FROM perf_peer p WHERE p.review_id = r.id AND p.status IN ('approved', 'submitted')) AS peers_total,
               (SELECT count(*)::int FROM perf_peer p WHERE p.review_id = r.id AND p.status = 'nominated') AS peers_pending
          FROM perf_review r JOIN employee e ON e.id = r.employee_id
          LEFT JOIN employee m ON m.id = r.manager_employee_id
          LEFT JOIN employee_assignment a ON a.employee_id = r.employee_id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
          LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
         WHERE r.cycle_id = %s AND (%s OR r.manager_employee_id = %s)
         ORDER BY e.name_en""", [c['id'], hr, user.employee_id])
    if not hr and not reviews:
        raise NotFoundError('review cycle not found')
    return {'cycle': c, 'reviews': [_ids(r, 'id', 'employee_id', 'manager_employee_id') for r in reviews]}


def launch_cycle(user: AuthUser, cycle_id: str) -> int:
    """Create one review per participant with their goals for the period and the competencies of their role."""
    _require_hr(user)
    company_id = company_id_of(user)
    with transaction.atomic():
        c = load_cycle(company_id, parse_uuid(str(cycle_id)), lock=True)
        if c['status'] != 'draft':
            raise ConflictError('this cycle was already launched')
        f = c['filter'] or {}
        emps = rows("""
            SELECT e.id, e.designation, e.grade, a.manager_id FROM employee e
              LEFT JOIN employee_assignment a ON a.employee_id = e.id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
             WHERE e.company_id = %s AND e.status = 'active'
               AND (cardinality(%s::uuid[]) = 0 OR a.org_unit_id = ANY(%s::uuid[]))
               AND (cardinality(%s::uuid[]) = 0 OR a.branch_id = ANY(%s::uuid[]))
               AND (%s::date IS NULL OR e.join_date IS NULL OR e.join_date <= %s::date)""",
                    [company_id, f.get('orgUnitIds') or [], f.get('orgUnitIds') or [], f.get('branchIds') or [], f.get('branchIds') or [],
                     f.get('joinedBefore'), f.get('joinedBefore')])
        if not emps:
            raise BadRequestError("no active employees match this cycle's filter")
        comps = rows('SELECT id, name, kind, description, designations, grades FROM perf_competency WHERE company_id = %s AND active', [company_id])
        for e in emps:
            rid = str(one('INSERT INTO perf_review (cycle_id, company_id, employee_id, manager_employee_id) VALUES (%s, %s, %s, %s) RETURNING id',
                          [c['id'], company_id, e['id'], e['manager_id']])['id'])
            execute("""
                INSERT INTO perf_review_item (review_id, item_type, ref_id, title, detail, weight)
                SELECT %s, 'goal', g.id, g.title, g.description, CASE WHEN g.weight > 0 THEN g.weight ELSE 1 END FROM perf_goal g
                 WHERE g.employee_id = %s AND g.scope = 'individual' AND g.status IN ('active', 'completed')
                   AND (g.due_date IS NULL OR g.due_date >= %s::date) AND (g.start_date IS NULL OR g.start_date <= %s::date)""",
                    [rid, e['id'], c['period_start'], c['period_end']])
            for cp in comps:
                if cp['designations'] and not (e['designation'] and e['designation'] in cp['designations']):
                    continue
                if cp['grades'] and not (e['grade'] and e['grade'] in cp['grades']):
                    continue
                kind = cp['kind'][:1].upper() + cp['kind'][1:]
                execute("INSERT INTO perf_review_item (review_id, item_type, ref_id, title, detail, weight) VALUES (%s, 'competency', %s, %s, %s, 1)",
                        [rid, cp['id'], cp['name'], f"{kind}{': ' + cp['description'] if cp['description'] else ''}"])
            emp = str(e['id'])
            notify(company_id=company_id, user_id=user_id_for_employee(emp), employee_id=emp, template='review.self_open',
                   title=f"{c['name']}: your self review is open",
                   body=f"Please complete it by {bs_label(c['self_due'])}." if c['self_due'] else 'Rate your goals and competencies and add a summary.',
                   payload={'reviewId': rid})
        execute("UPDATE perf_cycle SET status = 'self_review', launched_at = now() WHERE id = %s", [c['id']])
        audit('launch', 'perf_cycle', actor_id=user.id, row_id=c['id'], after={'participants': len(emps)})
    return len(emps)


def next_phase(c: dict) -> str:
    i = PHASES.index(c['status']) + 1
    if i < len(PHASES) and PHASES[i] == 'peer_review' and not c['peer_enabled']:
        i += 1
    return PHASES[min(i, len(PHASES) - 1)]


def advance_cycle(user: AuthUser, cycle_id: str) -> str:
    """Move the cycle to its next phase. Releasing fixes final ratings, shows results and turns training needs into nominations."""
    _require_hr(user)
    from hajiri.learning.services import create_review_nominations
    company_id = company_id_of(user)
    with transaction.atomic():
        c = load_cycle(company_id, parse_uuid(str(cycle_id)), lock=True)
        if c['status'] == 'draft':
            raise ConflictError('launch the cycle first')
        if c['status'] == 'closed':
            raise ConflictError('this cycle is closed')
        to = next_phase(c)
        execute('UPDATE perf_cycle SET status = %s WHERE id = %s', [to, c['id']])
        if to == 'manager_review':
            for m in rows("""SELECT DISTINCT manager_employee_id FROM perf_review WHERE cycle_id = %s AND manager_employee_id IS NOT NULL
                               AND manager_submitted_at IS NULL""", [c['id']]):
                mid = str(m['manager_employee_id'])
                notify(company_id=company_id, user_id=user_id_for_employee(mid), employee_id=mid, template='review.manager_open',
                       title=f"{c['name']}: manager reviews are open",
                       body=f"Review your team by {bs_label(c['manager_due'])}." if c['manager_due'] else 'Review your team.',
                       payload={'cycleId': c['id']})
        if to == 'peer_review':
            for p in rows("""SELECT DISTINCT p.reviewer_employee_id FROM perf_peer p JOIN perf_review r ON r.id = p.review_id
                              WHERE r.cycle_id = %s AND p.status = 'approved'""", [c['id']]):
                pid = str(p['reviewer_employee_id'])
                notify(company_id=company_id, user_id=user_id_for_employee(pid), employee_id=pid, template='review.peer_open',
                       title=f"{c['name']}: peer feedback requested", body='Colleagues asked for your feedback. Open Growth, Reviews to respond.',
                       payload={'cycleId': c['id']})
        if to == 'calibration':
            notify_role(company_id, 'hr', template='review.calibration', title=f"{c['name']}: ready for calibration",
                        body='Compare ratings across teams and set final ratings.', payload={'cycleId': c['id']})
        if to == 'released':
            execute('UPDATE perf_review SET final_rating = coalesce(final_rating, manager_score), released_at = now() WHERE cycle_id = %s', [c['id']])
            execute('UPDATE perf_cycle SET released_at = now() WHERE id = %s', [c['id']])
            for rv in rows('SELECT id, employee_id, recommendation FROM perf_review WHERE cycle_id = %s', [c['id']]):
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
    c = load_cycle(company_id_of(user), parse_uuid(str(cycle_id)))
    rs = rows("""
        SELECT r.id, r.employee_id, e.name_en, e.emp_code, coalesce(ou.name_en, 'No department') AS department, r.self_score, r.manager_score,
               r.final_rating, r.calibration_reason, r.recommendation
          FROM perf_review r JOIN employee e ON e.id = r.employee_id
          LEFT JOIN employee_assignment a ON a.employee_id = r.employee_id AND a.valid @> (now() AT TIME ZONE 'Asia/Kathmandu')::date
          LEFT JOIN org_unit ou ON ou.id = a.org_unit_id
         WHERE r.cycle_id = %s ORDER BY department, e.name_en""", [c['id']])
    distribution: dict[str, list[int]] = {}
    overall = [0, 0, 0, 0, 0]
    for r in rs:
        _ids(r, 'id', 'employee_id')
        v = r['final_rating'] if r['final_rating'] is not None else r['manager_score']
        if v is None:
            continue
        b = min(5, max(1, js_round(num(v))))
        distribution.setdefault(r['department'] or 'No department', [0, 0, 0, 0, 0])[b - 1] += 1
        overall[b - 1] += 1
    return {'cycle': c, 'reviews': rs, 'distribution': {'overall': overall, 'byDepartment': distribution}}


# ---------------------------------------------------------------- reviews

def list_reviews(user: AuthUser, scope: str = 'mine') -> list[dict]:
    scope = parse(Enum(('mine', 'team'), default='mine'), scope or 'mine')
    if not user.employee_id:
        return []
    out = rows("""
        SELECT r.id, r.cycle_id, c.name AS cycle_name, c.status AS cycle_status, c.period_start, c.period_end, c.self_due, c.manager_due, c.rating_labels,
               r.employee_id, e.name_en, e.emp_code, e.designation, r.self_submitted_at, r.manager_submitted_at, r.released_at, r.acknowledged_at,
               CASE WHEN %s = 'team' OR r.released_at IS NOT NULL THEN r.final_rating END AS final_rating,
               CASE WHEN %s = 'team' THEN r.manager_score END AS manager_score, r.self_score
          FROM perf_review r JOIN perf_cycle c ON c.id = r.cycle_id JOIN employee e ON e.id = r.employee_id
         WHERE r.company_id = %s AND c.status <> 'draft' AND (CASE WHEN %s = 'mine' THEN r.employee_id = %s ELSE r.manager_employee_id = %s END)
         ORDER BY c.created_at DESC, e.name_en""", [scope, scope, company_id_of(user), scope, user.employee_id, user.employee_id])
    return [_ids(r, 'id', 'cycle_id', 'employee_id') for r in out]


def review_detail(user: AuthUser, review_id: str) -> dict:
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(str(review_id)))
    role = viewer_role(user, rv)
    if not role or rv['cycle']['status'] == 'draft':
        raise NotFoundError('review not found')
    row = _ids(one("""SELECT r.*, e.name_en, e.emp_code, e.designation, m.name_en AS manager_name FROM perf_review r JOIN employee e ON e.id = r.employee_id
                       LEFT JOIN employee m ON m.id = r.manager_employee_id WHERE r.id = %s""", [rv['id']]),
               'id', 'cycle_id', 'company_id', 'employee_id', 'manager_employee_id', 'calibrated_by')
    items = [_ids(i, 'id', 'ref_id') for i in rows("""SELECT id, item_type, ref_id, title, detail, weight, self_rating, self_comment, manager_rating, manager_comment
                                                      FROM perf_review_item WHERE review_id = %s ORDER BY item_type DESC, title""", [rv['id']])]
    peers = [_ids(p, 'id', 'reviewer_employee_id') for p in rows("""
        SELECT p.id, p.reviewer_employee_id, e.name_en AS reviewer_name, p.status, p.rating, p.strengths, p.improvements, p.submitted_at
          FROM perf_peer p JOIN employee e ON e.id = p.reviewer_employee_id WHERE p.review_id = %s ORDER BY e.name_en""", [rv['id']])]
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
                                       'status': 'approved' if p['status'] == 'submitted' else p['status']} for p in peers],
                      'aggregate': peer_aggregate(peers, rv['cycle']['peer_min']) if released else None},
            'journal': [],
            'can': {'editSelf': not rv['self_submitted_at'] and phase in ('self_review', 'manager_review'),
                    'nominatePeers': rv['cycle']['peer_enabled'] and phase in OPEN_PHASES,
                    'acknowledge': released and not rv['acknowledged_at'], 'editManager': False, 'calibrate': False},
        }
    # Manager's journal: a manager sees their own notes for the period; HR sees everyone's.
    journal = rows("""SELECT j.id, j.note_date, j.tag, j.note, u.name AS author_name FROM perf_journal j JOIN app_user u ON u.id = j.author_id
                       WHERE j.employee_id = %s AND j.note_date BETWEEN %s AND %s AND (%s OR j.author_id = %s) ORDER BY j.note_date""",
                   [rv['employee_id'], rv['cycle']['period_start'], rv['cycle']['period_end'], role == 'hr', user.id])
    manager_open = phase in OPEN_PHASES and not rv['manager_submitted_at'] and (bool(rv['self_submitted_at']) or phase != 'self_review')
    return {
        'role': role, 'cycle': rv['cycle'], 'review': row, 'items': items,
        'peers': {'nominations': peers, 'aggregate': peer_aggregate(peers, 1)},
        'journal': journal,
        'can': {'editSelf': False,
                'editManager': manager_open and (role == 'hr' or rv['manager_employee_id'] == user.employee_id or not rv['manager_employee_id']),
                'nominatePeers': rv['cycle']['peer_enabled'] and phase in OPEN_PHASES,
                'calibrate': role == 'hr' and phase == 'calibration', 'acknowledge': False},
    }


def _save_ratings(review_id: str, side: str, ratings: list[dict]) -> None:
    for it in ratings:
        if not execute(f'UPDATE perf_review_item SET {side}_rating = %s, {side}_comment = %s WHERE id = %s AND review_id = %s',
                       [it['rating'], it.get('comment'), it['id'], review_id]):
            raise BadRequestError('an item does not belong to this review')


def _score_of(review_id: str, side: str, c: dict, require_all: bool) -> float | None:
    items = rows(f'SELECT item_type, weight, {side}_rating AS rating FROM perf_review_item WHERE review_id = %s', [review_id])
    if require_all and any(i['rating'] is None for i in items):
        raise BadRequestError('rate every goal and competency before submitting')
    return overall_score(items, num(c['goals_weight']), num(c['competency_weight']))


SELF_BODY = Obj({'items': Arr(ITEM_RATINGS.item, max=100, default=list), 'summary': Str(trim=True, max=4000, nullish=True),
                 'submit': Bool(default=False)})
MANAGER_BODY = Obj({'items': Arr(ITEM_RATINGS.item, max=100, default=list), 'summary': Str(trim=True, max=4000, nullish=True),
                    'recommendation': Obj(RECOMMENDATION.fields, optional=True), 'submit': Bool(default=False)})


def save_self(user: AuthUser, review_id: str, body: dict) -> None:
    b = parse(SELF_BODY, body)
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(str(review_id)))
    if not user.employee_id or rv['employee_id'] != user.employee_id:
        raise NotFoundError('review not found')
    if rv['cycle']['status'] not in ('self_review', 'manager_review'):
        raise ConflictError('the self review is closed')
    if rv['self_submitted_at']:
        raise ConflictError('you already submitted your self review')
    with transaction.atomic():
        _save_ratings(rv['id'], 'self', b['items'])
        if 'summary' in b:
            execute('UPDATE perf_review SET self_summary = %s WHERE id = %s', [b['summary'], rv['id']])
        if b['submit']:
            score = _score_of(rv['id'], 'self', rv['cycle'], True)
            execute('UPDATE perf_review SET self_submitted_at = now(), self_score = %s WHERE id = %s', [score, rv['id']])
            if rv['manager_employee_id']:
                m = rv['manager_employee_id']
                notify(company_id=company_id, user_id=user_id_for_employee(m), employee_id=m, template='review.self_submitted',
                       title='Self review submitted', body=f"{user.name} submitted their self review for {rv['cycle']['name']}.",
                       payload={'reviewId': rv['id']})


def save_manager(user: AuthUser, review_id: str, body: dict) -> None:
    b = parse(MANAGER_BODY, body)
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(str(review_id)))
    role = viewer_role(user, rv)
    if not role or role == 'self':
        raise NotFoundError('review not found')
    if role == 'manager' and rv['manager_employee_id'] and rv['manager_employee_id'] != user.employee_id:
        raise ForbiddenError('the direct manager writes this review')
    phase = rv['cycle']['status']
    if phase not in OPEN_PHASES:
        raise ConflictError('manager reviews are closed')
    if phase == 'self_review' and not rv['self_submitted_at']:
        raise ConflictError('wait for the self review, or until the manager review phase starts')
    if rv['manager_submitted_at']:
        raise ConflictError('the manager review was already submitted')
    rec = b.get('recommendation')
    if rec and rec['trainingCourseIds']:
        n = one('SELECT count(*)::int AS n FROM lrn_course WHERE company_id = %s AND id = ANY(%s::uuid[])', [company_id, rec['trainingCourseIds']])['n']
        if n != len(set(rec['trainingCourseIds'])):
            raise BadRequestError('a recommended course was not found')
    with transaction.atomic():
        _save_ratings(rv['id'], 'manager', b['items'])
        if 'summary' in b:
            execute('UPDATE perf_review SET manager_summary = %s WHERE id = %s', [b['summary'], rv['id']])
        if rec is not None:
            execute('UPDATE perf_review SET recommendation = %s WHERE id = %s', [json.dumps(rec), rv['id']])
        if b['submit']:
            score = _score_of(rv['id'], 'manager', rv['cycle'], True)
            execute('UPDATE perf_review SET manager_submitted_at = now(), manager_score = %s WHERE id = %s', [score, rv['id']])
            audit('submit', 'perf_review', actor_id=user.id, row_id=rv['id'], after={'managerScore': score})


CALIBRATE_BODY = Obj({'finalRating': Num(min=1, max=5, coerce=True),
                      'reason': Str(trim=True, max=1000, refine=(lambda s: len(s) >= 5, 'explain the change in a few words'))})


def calibrate(user: AuthUser, review_id: str, body: dict) -> None:
    _require_hr(user)
    b = parse(CALIBRATE_BODY, body)
    rv = load_review(company_id_of(user), parse_uuid(str(review_id)))
    if rv['cycle']['status'] != 'calibration':
        raise ConflictError('final ratings are set during calibration')
    fr = round2(b['finalRating'])
    with transaction.atomic():
        execute('UPDATE perf_review SET final_rating = %s, calibrated_by = %s, calibration_reason = %s WHERE id = %s',
                [fr, user.id, b['reason'], rv['id']])
        audit('calibrate', 'perf_review', actor_id=user.id, row_id=rv['id'],
              before={'finalRating': None if rv['final_rating'] is None else str(rv['final_rating']),
                      'managerScore': None if rv['manager_score'] is None else str(rv['manager_score'])},
              after={'finalRating': fr, 'reason': b['reason']})


def acknowledge(user: AuthUser, review_id: str, body: dict | None = None) -> None:
    b = parse(Obj({'comment': Str(trim=True, max=2000, nullish=True)}), body or {})
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(str(review_id)))
    if not user.employee_id or rv['employee_id'] != user.employee_id:
        raise NotFoundError('review not found')
    if not rv['released_at']:
        raise ConflictError('the review has not been released yet')
    if rv['acknowledged_at']:
        raise ConflictError('already acknowledged')
    with transaction.atomic():
        execute('UPDATE perf_review SET acknowledged_at = now(), employee_comment = %s WHERE id = %s', [b.get('comment'), rv['id']])
        if rv['manager_employee_id']:
            m = rv['manager_employee_id']
            notify(company_id=company_id, user_id=user_id_for_employee(m), employee_id=m, template='review.acknowledged',
                   title='Review acknowledged',
                   body=f"{user.name} acknowledged their {rv['cycle']['name']} review{' with a comment' if b.get('comment') else ''}.",
                   payload={'reviewId': rv['id']})


# ---------------------------------------------------------------- peers (360)

def nominate_peers(user: AuthUser, review_id: str, body: dict) -> int:
    b = parse(Obj({'employeeIds': Arr(Uuid(), min=1, max=10)}), body)
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(str(review_id)))
    role = viewer_role(user, rv)
    if not role:
        raise NotFoundError('review not found')
    if not rv['cycle']['peer_enabled']:
        raise ConflictError('this cycle has no peer reviews')
    if rv['cycle']['status'] not in OPEN_PHASES:
        raise ConflictError('peer nominations are closed')
    status = 'nominated' if role == 'self' else 'approved'
    added = 0
    with transaction.atomic():
        for eid in dict.fromkeys(b['employeeIds']):
            if eid == rv['employee_id']:
                raise BadRequestError('you cannot review yourself')
            if not one("SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s AND status = 'active'", [eid, company_id]):
                raise NotFoundError('colleague not found')
            added += execute('INSERT INTO perf_peer (review_id, reviewer_employee_id, status, nominated_by) VALUES (%s, %s, %s, %s) ON CONFLICT DO NOTHING',
                             [rv['id'], eid, status, user.id])
        if status == 'nominated' and added and rv['manager_employee_id']:
            m = rv['manager_employee_id']
            notify(company_id=company_id, user_id=user_id_for_employee(m), employee_id=m, template='review.peers_nominated',
                   title='Peer reviewers to approve',
                   body=f"{user.name} nominated {added} peer reviewer{'' if added == 1 else 's'} for {rv['cycle']['name']}.",
                   payload={'reviewId': rv['id']})
    return added


def _load_peer(company_id: str, peer_id: str) -> dict:
    r = one('SELECT p.id, p.review_id, p.reviewer_employee_id, p.status FROM perf_peer p JOIN perf_review r ON r.id = p.review_id WHERE p.id = %s AND r.company_id = %s',
            [peer_id, company_id])
    if not r:
        raise NotFoundError('peer review not found')
    return _ids(r, 'id', 'review_id', 'reviewer_employee_id')


def decide_peer(user: AuthUser, peer_id: str, body: dict) -> dict:
    b = parse(Obj({'approve': Bool()}), body)
    company_id = company_id_of(user)
    p = _load_peer(company_id, parse_uuid(str(peer_id)))
    rv = load_review(company_id, p['review_id'])
    role = viewer_role(user, rv)
    if role not in ('manager', 'hr'):
        raise ForbiddenError('the manager approves peer reviewers')
    if p['status'] != 'nominated':
        raise ConflictError(f"already {p['status']}")
    with transaction.atomic():
        execute('UPDATE perf_peer SET status = %s WHERE id = %s', ['approved' if b['approve'] else 'declined', p['id']])
        if b['approve'] and rv['cycle']['status'] in ('manager_review', 'peer_review', 'self_review'):
            r = p['reviewer_employee_id']
            notify(company_id=company_id, user_id=user_id_for_employee(r), employee_id=r, template='review.peer_requested',
                   title='Peer feedback requested', body=f"You were asked to give feedback in {rv['cycle']['name']}. Open Growth, Reviews.",
                   payload={'peerId': p['id']})
    return rv


def remove_peer(user: AuthUser, peer_id: str) -> dict:
    company_id = company_id_of(user)
    p = _load_peer(company_id, parse_uuid(str(peer_id)))
    rv = load_review(company_id, p['review_id'])
    role = viewer_role(user, rv)
    if not role:
        raise NotFoundError('peer review not found')
    if p['status'] == 'submitted':
        raise ConflictError('this colleague already responded')
    if role == 'self' and p['status'] != 'nominated':
        raise ForbiddenError('ask your manager to remove an approved reviewer')
    execute('DELETE FROM perf_peer WHERE id = %s', [p['id']])
    return rv


def peer_requests(user: AuthUser) -> list[dict]:
    """Peer feedback I have been asked to give."""
    if not user.employee_id:
        return []
    out = rows("""
        SELECT p.id, p.status, p.rating, p.strengths, p.improvements, p.submitted_at, e.name_en AS employee_name, e.designation, c.name AS cycle_name,
               c.status AS cycle_status, c.peer_due, c.rating_labels
          FROM perf_peer p JOIN perf_review r ON r.id = p.review_id JOIN perf_cycle c ON c.id = r.cycle_id JOIN employee e ON e.id = r.employee_id
         WHERE p.reviewer_employee_id = %s AND r.company_id = %s AND p.status IN ('approved', 'submitted') AND c.status NOT IN ('draft', 'closed')
         ORDER BY p.status, c.created_at DESC""", [user.employee_id, company_id_of(user)])
    return [_ids(r, 'id') for r in out]


def submit_peer(user: AuthUser, peer_id: str, body: dict) -> None:
    b = parse(Obj({'rating': Int(min=1, max=5), 'strengths': Str(trim=True, max=2000, nullish=True),
                   'improvements': Str(trim=True, max=2000, nullish=True)}), body)
    company_id = company_id_of(user)
    p = _load_peer(company_id, parse_uuid(str(peer_id)))
    if not user.employee_id or p['reviewer_employee_id'] != user.employee_id:
        raise NotFoundError('peer review not found')
    if p['status'] != 'approved':
        raise ConflictError('you already responded' if p['status'] == 'submitted' else 'this request is not open')
    rv = load_review(company_id, p['review_id'])
    if rv['cycle']['status'] not in OPEN_PHASES:
        raise ConflictError('peer feedback is closed')
    execute("UPDATE perf_peer SET rating = %s, strengths = %s, improvements = %s, status = 'submitted', submitted_at = now() WHERE id = %s",
            [b['rating'], b.get('strengths'), b.get('improvements'), p['id']])


# ---------------------------------------------------------------- printable summary

_PRINT_CSS = """
body{font-family:ui-sans-serif,system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;font-size:13px;color:#17191e;margin:32px auto;max-width:860px;line-height:1.55;padding:0 24px}
h1{font-size:20px;margin:0;font-weight:600}h2{font-size:14px;margin:24px 0 8px}.sub{color:#6b707b}.meta{display:grid;grid-template-columns:repeat(3,1fr);gap:10px 24px;margin:18px 0}
.meta span{display:block;color:#6b707b;font-size:11.5px}table{width:100%;border-collapse:collapse}th{text-align:left;font-weight:500;color:#6b707b;font-size:12px;border-bottom:1px solid #e6e7ea;padding:6px 4px}
td{padding:6px 4px;border-bottom:1px solid #f0f0f2;vertical-align:top}.n{text-align:right}.c{color:#6b707b;font-size:12px}.box{border:1px solid #e6e7ea;border-radius:10px;padding:12px 14px;white-space:pre-wrap}
.final{font-size:22px;font-weight:600;color:#12773a}.sign{display:grid;grid-template-columns:repeat(3,1fr);gap:24px;margin-top:56px}.sign div{border-top:1px solid #9aa0aa;padding-top:6px;color:#6b707b}
@media print{body{margin:0 auto}}"""


def _dash(v):
    return '—' if v is None else v


def review_print_html(user: AuthUser, review_id: str) -> str:
    company_id = company_id_of(user)
    rv = load_review(company_id, parse_uuid(str(review_id)))
    role = viewer_role(user, rv)
    if not role or rv['cycle']['status'] == 'draft' or (role == 'self' and not rv['released_at']):
        raise NotFoundError('review not found')
    h = one("""SELECT e.name_en, e.emp_code, e.designation, m.name_en AS manager_name, co.name_en AS company, r.self_summary, r.manager_summary,
                      r.final_rating, r.manager_score, r.self_score, r.recommendation, r.employee_comment, r.acknowledged_at, r.calibration_reason
                 FROM perf_review r JOIN employee e ON e.id = r.employee_id JOIN company co ON co.id = r.company_id
                 LEFT JOIN employee m ON m.id = r.manager_employee_id WHERE r.id = %s""", [rv['id']])
    items = rows("""SELECT item_type, title, weight, self_rating, self_comment, manager_rating, manager_comment FROM perf_review_item
                     WHERE review_id = %s ORDER BY item_type DESC, title""", [rv['id']])
    cyc = rv['cycle']
    labels = cyc['rating_labels'] or []

    def label(n) -> str:
        if n is None:
            return '—'
        v = num(n)
        txt = f'{v:.0f}' if v.is_integer() else f'{v:.2f}'
        idx = min(5, max(1, js_round(v))) - 1
        return f"{txt} {esc(labels[idx] if idx < len(labels) else '')}"

    def table_rows(t: str) -> str:
        out = []
        for i in items:
            if i['item_type'] != t:
                continue
            comment = f'<div class="c">{esc(i["manager_comment"])}</div>' if i['manager_comment'] else ''
            weight = f'<td class="n">{esc(fmt_number(i["weight"]))}</td>' if t == 'goal' else ''
            out.append(f'<tr><td>{esc(i["title"])}{comment}</td>{weight}<td>{label(i["self_rating"])}</td><td>{label(i["manager_rating"])}</td></tr>')
        return ''.join(out)

    rec = None if role == 'self' else h['recommendation']
    rec_html = ''
    if rec is not None:
        parts = [f"Increment {esc(fmt_number(rec['incrementPct']))}%" if rec.get('incrementPct') is not None else None,
                 'Promotion' if rec.get('promotion') else None, 'Performance improvement plan' if rec.get('pip') else None,
                 esc(rec['notes']) if rec.get('notes') else None]
        rec_html = f"<h2>Recommendation</h2><div class=\"box\">{chr(10).join(p for p in parts if p) or '—'}</div>"
    cal_html = f'<h2>Calibration note</h2><div class="box">{esc(h["calibration_reason"])}</div>' if role == 'hr' and h['calibration_reason'] else ''
    ack_html = ''
    if h['acknowledged_at']:
        ack_html = (f'<h2>Employee\'s acknowledgement</h2><div class="box">Acknowledged on {esc(bs_label(h["acknowledged_at"]))}'
                    f'{chr(10) + esc(h["employee_comment"]) if h["employee_comment"] else ""}</div>')
    final = h['final_rating'] if h['final_rating'] is not None else h['manager_score']
    goals = table_rows('goal') or '<tr><td colspan="4" class="c">No goals in this review</td></tr>'
    comps = table_rows('competency') or '<tr><td colspan="3" class="c">No competencies in this review</td></tr>'
    return f"""<!doctype html><html lang="en"><head><meta charset="utf-8"><title>Review {esc(h['emp_code'])} {esc(cyc['name'])}</title><style>{_PRINT_CSS}</style></head><body>
<div class="sub">{esc(h['company'])}</div><h1>{esc(cyc['name'])}: performance review</h1>
<div class="meta"><div><span>Employee</span>{esc(h['name_en'])} ({esc(h['emp_code'])})</div><div><span>Designation</span>{esc(_dash(h['designation']))}</div><div><span>Manager</span>{esc(_dash(h['manager_name']))}</div>
<div><span>Period</span>{esc(bs_label(cyc['period_start']))} to {esc(bs_label(cyc['period_end']))}</div><div><span>Self score</span>{label(h['self_score'])}</div><div><span>Final rating</span><span class="final">{label(final)}</span></div></div>
<h2>Goals ({esc(fmt_number(cyc['goals_weight']))}%)</h2><table><thead><tr><th>Goal</th><th class="n">Weight</th><th>Self</th><th>Manager</th></tr></thead><tbody>{goals}</tbody></table>
<h2>Competencies ({esc(fmt_number(cyc['competency_weight']))}%)</h2><table><thead><tr><th>Competency</th><th>Self</th><th>Manager</th></tr></thead><tbody>{comps}</tbody></table>
<h2>Employee's summary</h2><div class="box">{esc(_dash(h['self_summary']))}</div>
<h2>Manager's summary</h2><div class="box">{esc(_dash(h['manager_summary']))}</div>
{rec_html}
{cal_html}
{ack_html}
<div class="sign"><div>Employee</div><div>Manager</div><div>HR</div></div></body></html>"""

