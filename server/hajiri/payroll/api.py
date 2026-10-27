"""
One function per route of payroll/routes.ts: (user, body or query, path params) -> the TS response body. Access
rules, validation messages, company scoping and audit entries follow the TS routes. The JSON endpoints
(api_views.py) and the pages (views.py) both call these.
"""
from __future__ import annotations

import json
import re
from decimal import localcontext

from django.db import transaction

from engines.bs_calendar import BsRangeError
from engines.payroll import formula_references, validate_formula
from engines.payroll.numeric import ENGINE_CONTEXT, D, ceil

from hajiri.core.audit import audit
from hajiri.core.auth import MODULE_ROLES, AuthUser, assert_manages, company_id_of, has_module, has_role
from hajiri.core.crypto import field_cipher
from hajiri.core.errors import BadRequestError, ConflictError, ForbiddenError, NotFoundError
from hajiri.core.schema import (Any_, Arr, Bool, Enum, Int, IsoDate, Money, Obj, Record, Str, Uuid, parse)

from . import service as svc
from ._db import q, q1, x
from .payslip_html import render_payslip_html
from .rules import STATUTORY_KEYS
from .salary_sheet import salary_sheet, ssf_sheet_csv
from .tax_savings import DECL_FIELDS, decide_tax_savings, pending_tax_savings, submit_tax_savings

_AMOUNT = lambda **kw: Money(refine=(lambda v: not v.startswith('-'), 'must not be negative'), **kw)  # noqa: E731
CODE_RE = r'^[A-Z][A-Z0-9_]{1,30}$'
RUN_TYPES = ('regular', 'off_cycle', 'arrears', 'supplementary', 'final_settlement')


def require_payroll(user: AuthUser) -> None:
    if not has_module(user, 'payroll'):
        raise ForbiddenError(f"requires role {' or '.join(MODULE_ROLES['payroll'])}")


def _cid(user: AuthUser) -> str:
    return company_id_of(user)


def _strs(r: dict, *keys: str) -> dict:
    for k in keys:
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


# ---------------------------------------------------------------- rule versions (plan §6.13)

def _is_url(v: str) -> bool:
    """zod .url() (new URL(v) parses): a scheme and, for http(s), a host."""
    from urllib.parse import urlsplit
    try:
        u = urlsplit(v)
    except ValueError:
        return False
    if not re.match(r'^[A-Za-z][A-Za-z0-9+.-]*$', u.scheme or '') or any(c.isspace() for c in v):
        return False
    return bool(u.netloc) if u.scheme.lower() in ('http', 'https') else True


def list_rules(user: AuthUser, query: dict) -> dict:
    require_payroll(user)
    qy = parse(Obj({'key': Str(optional=True), 'asOf': IsoDate(optional=True)}), query)
    r = q("""SELECT id, rule_key, jurisdiction, valid_from_ad, valid_to_ad, valid_from_bs, valid_to_bs, value, source_url, note, approved_by, approved_at,
                    supersedes_id, created_at
               FROM rule_version WHERE ($1::text IS NULL OR rule_key = $1)
                AND ($2::date IS NULL OR (valid_from_ad <= $2::date AND (valid_to_ad IS NULL OR valid_to_ad >= $2::date)))
              ORDER BY rule_key, valid_from_ad DESC""", [qy.get('key'), qy.get('asOf')])
    return {'rules': [{**_strs(x_, 'id', 'approved_by', 'supersedes_id'), 'locked': x_['rule_key'] in STATUTORY_KEYS} for x_ in r]}


@transaction.atomic
def create_rule(user: AuthUser, body: dict) -> dict:
    """Rule versions are jurisdiction-wide (not per company), so only an administrator may add them."""
    if not has_role(user):
        raise ForbiddenError('requires role admin')
    b = parse(Obj({'ruleKey': Str(min=1, max=60), 'validFromAd': IsoDate(), 'validToAd': IsoDate(nullish=True), 'validFromBs': Str(),
                   'validToBs': Str(nullish=True), 'value': Any_(optional=True),
                   'sourceUrl': Str(regex=r'^https?://', regex_message='source must be an http(s) link', optional=True),
                   'note': Str(max=500, optional=True), 'supersedesId': Uuid(optional=True)}), body)
    if b.get('sourceUrl') is not None and not _is_url(b['sourceUrl']):
        from hajiri.core.errors import ValidationError
        raise ValidationError([{'path': 'sourceUrl', 'message': 'Invalid URL'}])
    r = q1("""INSERT INTO rule_version (rule_key, valid_from_ad, valid_to_ad, valid_from_bs, valid_to_bs, value, source_url, note, supersedes_id)
              VALUES ($1, $2::date, $3::date, $4, $5, $6, $7, $8, $9) RETURNING id""",
           [b['ruleKey'], b['validFromAd'], b.get('validToAd'), b['validFromBs'], b.get('validToBs'), json.dumps(b.get('value')), b.get('sourceUrl'),
            b.get('note'), b.get('supersedesId')])
    if b.get('supersedesId'):
        x('UPDATE rule_version SET valid_to_ad = coalesce(valid_to_ad, $2::date - 1) WHERE id = $1', [b['supersedesId'], b['validFromAd']])
    audit('create', 'rule_version', actor_id=user.id, row_id=str(r['id']), after=b)
    return {'id': str(r['id'])}


@transaction.atomic
def approve_rule(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    rid = parse(Uuid(), id_)
    if not q1('UPDATE rule_version SET approved_by = $2, approved_at = now() WHERE id = $1 RETURNING id', [rid, user.id]):
        raise NotFoundError('rule not found')
    audit('approve', 'rule_version', actor_id=user.id, row_id=rid)
    return {'ok': True}


# ---------------------------------------------------------------- components, structures, assignments (plan §9.2)

COMPONENT_TYPES = ('earning', 'deduction', 'employer_contribution', 'statistical')


def component_fields() -> dict:
    return {'code': Str(regex=CODE_RE), 'nameEn': Str(min=1), 'nameNe': Str(optional=True), 'type': Enum(COMPONENT_TYPES),
            'formula': Str(max=500, nullish=True), 'condition': Str(max=500, nullish=True), 'dependsOnPaymentDays': Bool(default=False),
            'taxable': Bool(default=True), 'rounding': Enum(('paisa', 'rupee'), default='paisa'), 'sortOrder': Int(default=100),
            'active': Bool(default=True)}


def _partial(fields: dict) -> Obj:
    """zod .partial(): every key may be missing; defaults still fill missing keys (zod 4)."""
    for s in fields.values():
        s.optional = True
    return Obj(fields)


def list_components(user: AuthUser) -> dict:
    require_payroll(user)
    r = q('SELECT * FROM salary_component WHERE company_id = $1 ORDER BY sort_order, code', [_cid(user)])
    return {'components': [_strs(c, 'company_id') for c in r]}


_MISSING = object()


def check_formulas(company_id: str, code: str, formula=_MISSING, condition=_MISSING) -> None:
    all_ = q('SELECT code, formula, condition FROM salary_component WHERE company_id = $1', [company_id])
    known = [c['code'] for c in all_] + [code]
    for label, src in (('formula', formula), ('condition', condition)):
        if src is not _MISSING and src:
            err = validate_formula(src, known)
            if err:
                raise BadRequestError(f'{label}: {err}')
    # A component may not depend on itself, directly or through others.
    codes = set(known)
    graph: dict[str, list] = {c['code']: [c['formula'], c['condition']] for c in all_}
    cur = graph.get(code, [None, None])
    graph[code] = [formula if formula is not _MISSING else cur[0], condition if condition is not _MISSING else cur[1]]
    deps: dict[str, list[str]] = {}
    for c, srcs in graph.items():
        refs: list[str] = []
        for src in srcs:
            if src:
                try:
                    for r in formula_references(src):
                        if r in codes and r not in refs:
                            refs.append(r)
                except Exception:  # noqa: BLE001 - an invalid stored formula is reported when it runs
                    pass
        deps[c] = refs
    path: list[str] = []
    state: dict[str, int] = {}

    def visit(c: str):
        if state.get(c) == 2:
            return None
        if state.get(c) == 1:
            return [*path[path.index(c):], c]
        state[c] = 1
        path.append(c)
        for d in deps.get(c, []):
            cyc = visit(d)
            if cyc:
                return cyc
        path.pop()
        state[c] = 2
        return None
    cycle = visit(code)
    if cycle:
        raise BadRequestError(f"formula refers back to itself: {' → '.join(cycle)}")


@transaction.atomic
def create_component(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = parse(Obj(component_fields()), body)
    cid = _cid(user)
    check_formulas(cid, b['code'], b.get('formula'), b.get('condition'))
    x("""INSERT INTO salary_component (company_id, code, name_en, name_ne, type, formula, condition, depends_on_payment_days, taxable, rounding, sort_order, active)
         VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12)""",
      [cid, b['code'], b['nameEn'], b.get('nameNe'), b['type'], b.get('formula'), b.get('condition'), b['dependsOnPaymentDays'], b['taxable'],
       b['rounding'], b['sortOrder'], b['active']])
    audit('create', 'salary_component', actor_id=user.id, row_id=b['code'], after=b)
    return {'code': b['code']}


@transaction.atomic
def update_component(user: AuthUser, code: str, body: dict) -> dict:
    require_payroll(user)
    b = parse(_partial(component_fields()), body)
    cid = _cid(user)
    cur = q1('SELECT * FROM salary_component WHERE company_id = $1 AND code = $2', [cid, code])
    if not cur:
        raise NotFoundError('component not found')
    if cur['statutory'] and ('formula' in b or 'type' in b):
        raise ForbiddenError('statutory components are locked (plan §13.8)')
    check_formulas(cid, code, b['formula'] if 'formula' in b else _MISSING, b['condition'] if 'condition' in b else _MISSING)
    x("""UPDATE salary_component SET name_en = coalesce($3, name_en), name_ne = coalesce($4, name_ne), formula = CASE WHEN $5 THEN $6 ELSE formula END,
           condition = CASE WHEN $7 THEN $8 ELSE condition END, depends_on_payment_days = coalesce($9, depends_on_payment_days), taxable = coalesce($10, taxable),
           rounding = coalesce($11, rounding), sort_order = coalesce($12, sort_order), active = coalesce($13, active)
         WHERE company_id = $1 AND code = $2""",
      [cid, code, b.get('nameEn'), b.get('nameNe'), 'formula' in b, b.get('formula'), 'condition' in b, b.get('condition'),
       b.get('dependsOnPaymentDays'), b.get('taxable'), b.get('rounding'), b.get('sortOrder'), b.get('active')])
    audit('update', 'salary_component', actor_id=user.id, row_id=code, before=_strs(dict(cur), 'company_id'), after=b)
    return {'ok': True}


def list_structures(user: AuthUser) -> dict:
    require_payroll(user)
    r = q("""SELECT s.id, s.code, s.name_en,
                    (SELECT json_agg(json_build_object('id', v.id, 'version', v.version, 'components', v.components, 'frozenAt', v.frozen_at) ORDER BY v.version)
                       FROM salary_structure_version v WHERE v.structure_id = s.id) AS versions
               FROM salary_structure s WHERE s.company_id = $1 ORDER BY s.code""", [_cid(user)])
    return {'structures': [_strs(s, 'id') for s in r]}


_COMPONENT_LIST = lambda: Arr(Obj({'code': Str()}), min=1)  # noqa: E731


@transaction.atomic
def create_structure(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = parse(Obj({'code': Str(min=1, max=20), 'nameEn': Str(min=1), 'components': _COMPONENT_LIST()}), body)
    s = q1('INSERT INTO salary_structure (company_id, code, name_en) VALUES ($1, $2, $3) RETURNING id', [_cid(user), b['code'], b['nameEn']])
    v = q1('INSERT INTO salary_structure_version (structure_id, version, components) VALUES ($1, 1, $2) RETURNING id',
           [s['id'], json.dumps(b['components'])])
    audit('create', 'salary_structure', actor_id=user.id, row_id=str(s['id']), after=b)
    return {'id': str(s['id']), 'versionId': str(v['id'])}


@transaction.atomic
def add_structure_version(user: AuthUser, id_: str, body: dict) -> dict:
    require_payroll(user)
    sid = parse(Uuid(), id_)
    b = parse(Obj({'components': _COMPONENT_LIST()}), body)
    if not q1('SELECT 1 AS x FROM salary_structure WHERE id = $1 AND company_id = $2', [sid, _cid(user)]):
        raise NotFoundError('structure not found')
    v = q1("""INSERT INTO salary_structure_version (structure_id, version, components)
              VALUES ($1, (SELECT coalesce(max(version), 0) + 1 FROM salary_structure_version WHERE structure_id = $1), $2) RETURNING id, version""",
           [sid, json.dumps(b['components'])])
    audit('create', 'salary_structure_version', actor_id=user.id, row_id=str(v['id']), after=b)
    return {'id': str(v['id']), 'version': v['version']}


def list_assignments(user: AuthUser, query: dict) -> dict:
    require_payroll(user)
    qy = parse(Obj({'employeeId': Uuid(optional=True)}), query)
    r = q("""SELECT a.employee_id, e.emp_code, e.name_en, a.structure_version_id, s.code AS structure_code, v.version, a.valid::text, a.base_amounts, a.created_at
               FROM salary_assignment a JOIN employee e ON e.id = a.employee_id JOIN salary_structure_version v ON v.id = a.structure_version_id
               JOIN salary_structure s ON s.id = v.structure_id
              WHERE e.company_id = $1 AND ($2::uuid IS NULL OR a.employee_id = $2::uuid) ORDER BY e.emp_code, lower(a.valid) DESC""",
          [_cid(user), qy.get('employeeId')])
    return {'assignments': [_strs(a, 'employee_id', 'structure_version_id') for a in r]}


def create_assignment(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = parse(Obj({'employeeId': Uuid(), 'structureVersionId': Uuid(), 'from': IsoDate(), 'baseAmounts': Record(_AMOUNT())}), body)
    cid = _cid(user)
    if not q1('SELECT 1 AS x FROM employee WHERE id = $1 AND company_id = $2', [b['employeeId'], cid]):
        raise NotFoundError('employee not found')
    sv = q1("""SELECT v.components FROM salary_structure_version v JOIN salary_structure s ON s.id = v.structure_id
                WHERE v.id = $1 AND s.company_id = $2""", [b['structureVersionId'], cid])
    if not sv:
        raise NotFoundError('salary structure version not found')
    in_structure = {c['code'] for c in sv['components']}
    unknown = [c for c in b['baseAmounts'] if c not in in_structure]
    if unknown:
        raise BadRequestError(f"not components of this salary structure: {', '.join(unknown)}")
    if not b['baseAmounts']:
        raise BadRequestError('baseAmounts needs at least one component amount')
    with transaction.atomic():
        # Close the current open assignment the day before and open the new one (effective-dated, plan §9.1).
        x("""UPDATE salary_assignment SET valid = daterange(lower(valid), $2::date) WHERE employee_id = $1 AND upper_inf(valid) AND lower(valid) < $2::date""",
          [b['employeeId'], b['from']])
        x('DELETE FROM salary_assignment WHERE employee_id = $1 AND lower(valid) >= $2::date', [b['employeeId'], b['from']])
        x("""INSERT INTO salary_assignment (employee_id, structure_version_id, valid, base_amounts, created_by)
             VALUES ($1, $2, daterange($3::date, NULL), $4, $5)""",
          [b['employeeId'], b['structureVersionId'], b['from'], json.dumps(b['baseAmounts']), user.id])
        x('UPDATE salary_structure_version SET frozen_at = coalesce(frozen_at, now()) WHERE id = $1', [b['structureVersionId']])
        audit('create', 'salary_assignment', actor_id=user.id, row_id=b['employeeId'], after={**b, 'baseAmounts': '***'})
    return {'ok': True}


# ---------------------------------------------------------------- declarations and loans

def _decl_row(r: dict | None) -> dict | None:
    if not r:
        return None
    r = _strs(dict(r), 'id', 'employee_id', 'verified_by')
    for k in ('life_premium', 'health_premium', 'cit_monthly', 'medical_cost', 'other_retirement_annual', 'donation', 'cit_prev_monthly'):
        if r.get(k) is not None:
            r[k] = str(r[k])
    return r


def get_declaration(user: AuthUser, query: dict) -> dict:
    qy = parse(Obj({'employeeId': Uuid(optional=True), 'fiscalYearStart': Int(coerce=True)}), query)
    employee_id = qy.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    assert_manages(user, employee_id)
    if employee_id != user.employee_id and not has_module(user, 'payroll'):
        raise ForbiddenError()
    cid = _cid(user)
    d = _decl_row(q1("""SELECT d.* FROM tax_declaration d JOIN employee e ON e.id = d.employee_id
                         WHERE d.employee_id = $1 AND d.fiscal_year_start = $2 AND e.company_id = $3""", [employee_id, qy['fiscalYearStart'], cid]))
    pending = (pending_tax_savings(employee_id, qy['fiscalYearStart'])
               if d or q1('SELECT 1 AS x FROM employee WHERE id = $1 AND company_id = $2', [employee_id, cid]) else None)
    return {'declaration': d, 'pending': pending}


def put_declaration(user: AuthUser, body: dict) -> dict:
    """
    HR entering a declaration for someone else records it as verified (HR is the verifier). Everyone's own
    declaration, including an HR user's, goes to HR as a tax savings request and only counts once approved.
    """
    b = parse(Obj({'employeeId': Uuid(optional=True), 'fiscalYearStart': Int(), **{k: _AMOUNT(default='0') for k in DECL_FIELDS},
                   'submit': Bool(default=True)}), body)
    employee_id = b.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    assert_manages(user, employee_id)
    if employee_id != user.employee_id and not has_module(user, 'payroll'):
        raise ForbiddenError()
    cid = _cid(user)
    if not q1('SELECT 1 AS x FROM employee WHERE id = $1 AND company_id = $2', [employee_id, cid]):
        raise NotFoundError('employee not found')
    values = {k: b[k] for k in DECL_FIELDS}
    fy = b['fiscalYearStart']
    if employee_id == user.employee_id:
        if not b['submit']:
            raise BadRequestError('drafts are not kept; submit the declaration when it is ready')
        with transaction.atomic():
            x('SELECT pg_advisory_xact_lock(hashtext($1))', [f'tax_savings:{employee_id}'])
            req = submit_tax_savings(user, cid, employee_id, fy, values)
        return {'ok': True, 'requestId': req['id'], 'status': 'pending'}
    status = 'verified' if b['submit'] else 'draft'
    with transaction.atomic():
        x("""INSERT INTO tax_declaration (employee_id, fiscal_year_start, life_premium, health_premium, cit_monthly, medical_cost, other_retirement_annual, donation, status, verified_by, verified_at)
             VALUES ($1, $2, $3, $4, $5, $6, $7, $10, $8, $9, CASE WHEN $8 = 'verified' THEN now() END)
             ON CONFLICT (employee_id, fiscal_year_start) DO UPDATE SET life_premium = EXCLUDED.life_premium, health_premium = EXCLUDED.health_premium, cit_monthly = EXCLUDED.cit_monthly,
               medical_cost = EXCLUDED.medical_cost, other_retirement_annual = EXCLUDED.other_retirement_annual, donation = EXCLUDED.donation, cit_from_month = NULL, cit_prev_monthly = 0,
               status = EXCLUDED.status, verified_by = EXCLUDED.verified_by, verified_at = EXCLUDED.verified_at, updated_at = now()""",
          [employee_id, fy, b['lifePremium'], b['healthPremium'], b['citMonthly'], b['medicalCost'], b['otherRetirementAnnual'], status,
           user.id if b['submit'] else None, b['donation']])
        audit('upsert', 'tax_declaration', actor_id=user.id, row_id=employee_id, after=b)
    return {'ok': True, 'status': status}


def verify_declaration(user: AuthUser, employee_id: str, body: dict) -> dict:
    require_payroll(user)
    eid = parse(Uuid(), employee_id)
    b = parse(Obj({'fiscalYearStart': Int(), 'decision': Enum(('verified', 'rejected')), 'reason': Str(trim=True, max=500, optional=True)}), body)
    cid = _cid(user)
    if not q1('SELECT 1 AS x FROM employee WHERE id = $1 AND company_id = $2', [eid, cid]):
        raise NotFoundError('declaration not found')
    pending = pending_tax_savings(eid, b['fiscalYearStart'])
    with transaction.atomic():
        if pending:
            decide_tax_savings(user, pending, 'approved' if b['decision'] == 'verified' else 'rejected', b.get('reason'))
            return {'ok': True}
        r = q1("""UPDATE tax_declaration d SET status = $3, verified_by = $4, verified_at = now() FROM employee e
                   WHERE d.employee_id = e.id AND d.employee_id = $1 AND d.fiscal_year_start = $2 AND e.company_id = $5 RETURNING d.id""",
               [eid, b['fiscalYearStart'], b['decision'], user.id, cid])
        if not r:
            raise NotFoundError('declaration not found')
        audit(b['decision'], 'tax_declaration', actor_id=user.id, row_id=eid)
    return {'ok': True}


def list_loans(user: AuthUser, query: dict) -> dict:
    require_payroll(user)
    qy = parse(Obj({'employeeId': Uuid(optional=True)}), query)
    r = q("""SELECT l.*, e.emp_code, coalesce((SELECT sum(i.amount) FROM loan_installment i WHERE i.loan_id = l.id), 0)::text AS repaid
               FROM loan l JOIN employee e ON e.id = l.employee_id
              WHERE e.company_id = $1 AND ($2::uuid IS NULL OR l.employee_id = $2::uuid) ORDER BY l.created_at DESC""", [_cid(user), qy.get('employeeId')])
    return {'loans': [_strs(ln, 'id', 'employee_id', 'created_by') for ln in r]}


@transaction.atomic
def create_loan(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = parse(Obj({'employeeId': Uuid(), 'kind': Enum(('loan', 'advance'), default='loan'), 'principal': Money(), 'emi': Money(),
                   'startBsYear': Int(), 'startBsMonth': Int(min=1, max=12), 'note': Str(optional=True)}), body)
    with localcontext(ENGINE_CONTEXT):
        if not D(b['emi']) > 0 or not D(b['principal']) > 0:
            raise BadRequestError('principal and emi must be positive')
        installments = int(ceil(D(b['principal']) / D(b['emi'])))
    r = q1("""INSERT INTO loan (employee_id, kind, principal, emi, start_bs_year, start_bs_month, installments, note, created_by)
              SELECT $1, $2, $3, $4, $5, $6, $7, $8, $9 FROM employee WHERE id = $1 AND company_id = $10 RETURNING id""",
           [b['employeeId'], b['kind'], b['principal'], b['emi'], b['startBsYear'], b['startBsMonth'], installments, b.get('note'), user.id, _cid(user)])
    if not r:
        raise NotFoundError('employee not found')
    audit('create', 'loan', actor_id=user.id, row_id=str(r['id']), after=b)
    return {'id': str(r['id']), 'installments': installments}


# ---------------------------------------------------------------- periods and runs (plan §13.6 wizard)

def list_periods(user: AuthUser) -> dict:
    require_payroll(user)
    r = q('SELECT * FROM payroll_period WHERE company_id = $1 ORDER BY bs_year DESC, bs_month DESC', [_cid(user)])
    return {'periods': [svc._period(p) for p in r]}


@transaction.atomic
def create_period(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = parse(Obj({'bsYear': Int(), 'bsMonth': Int(min=1, max=12)}), body)
    try:
        return {'period': svc.ensure_period(_cid(user), b['bsYear'], b['bsMonth'])}
    except BsRangeError as e:
        raise BadRequestError(f"BS {b['bsYear']}-{b['bsMonth']} is outside the supported calendar: {e}")


def list_runs(user: AuthUser) -> dict:
    require_payroll(user)
    r = q("""SELECT r.id, r.period_id, p.bs_year, p.bs_month, r.run_type, r.status, r.totals, r.maker_id, r.checker_id, r.computed_at, r.approved_at,
                    r.locked_at, r.disbursed_at, r.filed_at, r.created_at
               FROM payroll_run r JOIN payroll_period p ON p.id = r.period_id WHERE r.company_id = $1 ORDER BY r.created_at DESC""", [_cid(user)])
    return {'runs': [_strs(x_, 'id', 'period_id', 'maker_id', 'checker_id') for x_ in r]}


@transaction.atomic
def create_run(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = parse(Obj({'periodId': Uuid(), 'runType': Enum(RUN_TYPES, default='regular'), 'referencesRunId': Uuid(optional=True),
                   'employeeIds': Arr(Uuid(), optional=True), 'noticePay': Money(optional=True), 'notes': Str(max=1000, optional=True)}), body)
    cid = _cid(user)
    p = svc.get_period(cid, b['periodId'])
    if b['runType'] == 'regular':
        dup = q1("SELECT id, locked_at FROM payroll_run WHERE period_id = $1 AND run_type = 'regular' ORDER BY created_at DESC LIMIT 1", [p['id']])
        if dup:
            raise ConflictError('a locked regular run already exists for this period; use a supplementary or arrears run' if dup['locked_at']
                                else f"a regular run already exists for this period ({dup['id']}); continue that run instead")
    if b['runType'] == 'arrears' and not b.get('referencesRunId'):
        raise BadRequestError('arrears runs need referencesRunId')
    emp_ids = b.get('employeeIds')
    if b['runType'] == 'final_settlement':
        if not (emp_ids and len(emp_ids) == 1):
            raise BadRequestError('final settlement runs need exactly one employeeId')
        ex = q1('SELECT exit_date::text FROM employee WHERE id = $1 AND company_id = $2', [emp_ids[0], cid])
        if not ex:
            raise NotFoundError('employee not found')
        if not ex['exit_date']:
            raise BadRequestError('final settlement needs an exit date on the employee record')
        if ex['exit_date'] > p['end_ad']:
            raise BadRequestError('the exit date is after this period; pick the period containing the exit date')
    if emp_ids:
        own = q1('SELECT count(*)::int AS n FROM employee WHERE company_id = $1 AND id = ANY($2::uuid[])', [cid, emp_ids])
        if own['n'] != len(set(emp_ids)):
            raise NotFoundError('one or more employees not found')
    if b.get('referencesRunId'):
        ref = svc.get_run(cid, b['referencesRunId'])
        if not ref['locked_at']:
            raise BadRequestError('referenced run must be locked')
    params = {**({'employeeIds': emp_ids} if emp_ids is not None else {}), **({'noticePay': b['noticePay']} if b.get('noticePay') else {})}
    r = q1("""INSERT INTO payroll_run (company_id, period_id, run_type, references_run_id, params, notes, maker_id) VALUES ($1, $2, $3, $4, $5, $6, $7) RETURNING id""",
           [cid, p['id'], b['runType'], b.get('referencesRunId'), json.dumps(params), b.get('notes'), user.id])
    audit('create', 'payroll_run', actor_id=user.id, row_id=str(r['id']), after=b)
    return {'id': str(r['id'])}


@transaction.atomic
def create_bonus_run(user: AuthUser, body: dict) -> dict:
    require_payroll(user)
    b = parse(Obj({'periodId': Uuid(), 'fiscalYearStart': Int(), 'netProfit': _AMOUNT()}), body)
    cid = _cid(user)
    p = svc.get_period(cid, b['periodId'])
    alloc = svc.bonus_allocation(cid, b['fiscalYearStart'], b['netProfit'])
    params = {'fiscalYearStart': b['fiscalYearStart'], 'netProfit': b['netProfit'], 'pool': alloc['pool'], 'bonusByEmployee': alloc['byEmployee'],
              'detail': alloc['detail'], 'employeeIds': list(alloc['byEmployee'])}
    r = q1("INSERT INTO payroll_run (company_id, period_id, run_type, params, maker_id) VALUES ($1, $2, 'bonus', $3, $4) RETURNING id",
           [cid, p['id'], json.dumps(params), user.id])
    audit('create', 'payroll_run', actor_id=user.id, row_id=str(r['id']), after={'runType': 'bonus', 'pool': alloc['pool']})
    return {'id': str(r['id']), 'pool': alloc['pool'], 'allocation': alloc['detail']}


def run_payslips(run_id: str) -> list[dict]:
    r = q("""SELECT ps.id, ps.employee_id, e.emp_code, e.name_en, ps.paid_days, ps.lop_days, ps.ot_hours, ps.gross, ps.tds, ps.employee_statutory,
                    ps.employer_statutory, ps.total_deductions, ps.net, ps.flags, ps.published_at
               FROM payslip ps JOIN employee e ON e.id = ps.employee_id WHERE ps.run_id = $1
              ORDER BY (jsonb_array_length(ps.flags) > 0) DESC, e.emp_code""", [run_id])
    return [_strs(s, 'id', 'employee_id') for s in r]


def run_detail(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    cid = _cid(user)
    run = svc.get_run(cid, parse(Uuid(), id_))
    p = svc.get_period(cid, run['period_id'])
    return {'run': run, 'period': p, 'payslips': run_payslips(run['id'])}


def run_preflight(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    cid = _cid(user)
    with transaction.atomic():
        run = svc.get_run(cid, parse(Uuid(), id_))
        pf = svc.preflight(cid, run)
        if run['status'] in ('draft', 'preflight_failed'):
            x('UPDATE payroll_run SET status = $2, preflight = $3 WHERE id = $1', [run['id'], 'draft' if pf['ok'] else 'preflight_failed', json.dumps(pf)])
    return pf


@transaction.atomic
def run_lock_attendance(user: AuthUser, id_: str) -> dict:
    from hajiri.attendance.service import lock_attendance
    require_payroll(user)
    cid = _cid(user)
    run = svc.get_run(cid, parse(Uuid(), id_))
    p = svc.get_period(cid, run['period_id'])
    n = lock_attendance(cid, p['start_ad'], p['end_ad'])
    x('UPDATE payroll_period SET attendance_locked_at = coalesce(attendance_locked_at, now()) WHERE id = $1', [p['id']])
    audit('lock_attendance', 'payroll_period', actor_id=user.id, row_id=p['id'], after={'locked': n})
    return {'locked': n}


def add_inputs(user: AuthUser, id_: str, body: dict) -> dict:
    require_payroll(user)
    cid = _cid(user)
    run = svc.get_run(cid, parse(Uuid(), id_))
    if run['status'] not in ('draft', 'preflight_failed', 'computed', 'reviewed'):
        raise ConflictError(f"run is {run['status']}; inputs can no longer change")
    b = parse(Obj({'inputs': Arr(Obj({'employeeId': Uuid(), 'componentCode': Str(regex=CODE_RE), 'amount': Money(), 'note': Str(max=200, optional=True)}),
                                 min=1, max=5000)}), body)
    codes = {c['code'] for c in q("SELECT code FROM salary_component WHERE company_id = $1 AND active AND type IN ('earning', 'deduction')", [cid])}
    unknown = [c for c in dict.fromkeys(i['componentCode'] for i in b['inputs']) if c not in codes]
    if unknown:
        raise BadRequestError(f"unknown or inactive component code(s): {', '.join(unknown)}")
    with localcontext(ENGINE_CONTEXT):
        for i in b['inputs']:
            if not D(i['amount']) > 0:
                raise BadRequestError('input amounts must be positive')
    # A run scoped to named employees only pays them; an input for anyone else would be silently dropped.
    scoped_ids = run['params'].get('employeeIds')
    scoped = set(str(e) for e in scoped_ids) if isinstance(scoped_ids, list) else None
    outside = [e for e in dict.fromkeys(i['employeeId'] for i in b['inputs']) if e not in scoped] if scoped is not None else []
    if outside:
        n = len(scoped)
        raise BadRequestError(f"this run only includes {n} named {'employee' if n == 1 else 'employees'}; {len(outside)} input "
                              f"{'employee is' if len(outside) == 1 else 'employees are'} not in it")
    with transaction.atomic():
        added = 0
        for i in b['inputs']:
            added += x("""INSERT INTO payroll_input (run_id, employee_id, component_code, amount, note, created_by)
                          SELECT $1, $2, $3, $4, $5, $6 FROM employee WHERE id = $2 AND company_id = $7""",
                       [run['id'], i['employeeId'], i['componentCode'], i['amount'], i.get('note'), user.id, cid])
        if added != len(b['inputs']):
            raise NotFoundError('one or more employees do not belong to this company')
        # New inputs invalidate a maker review; the run must be recomputed and reviewed again.
        if run['status'] == 'reviewed':
            x("UPDATE payroll_run SET status = 'computed', reviewed_at = NULL WHERE id = $1", [run['id']])
        audit('inputs', 'payroll_run', actor_id=user.id, row_id=run['id'], after={'count': added})
    return {'added': added}


def list_inputs(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    run = svc.get_run(_cid(user), parse(Uuid(), id_))
    r = q("""SELECT i.id, i.employee_id, e.emp_code, i.component_code, i.amount, i.note FROM payroll_input i JOIN employee e ON e.id = i.employee_id
              WHERE i.run_id = $1 ORDER BY e.emp_code""", [run['id']])
    return {'inputs': [_strs(i, 'id', 'employee_id') for i in r]}


def compute(user: AuthUser, id_: str, body: dict | None) -> dict:
    require_payroll(user)
    b = parse(Obj({'overrideReason': Str(min=5, max=500, optional=True)}), body or {})
    override = {'reason': b['overrideReason'], 'roles': list(user.roles)} if b.get('overrideReason') else None
    return svc.compute_run(_cid(user), parse(Uuid(), id_), user.id, override)


def review(user: AuthUser, id_: str, body: dict | None) -> dict:
    require_payroll(user)
    b = parse(Obj({'notes': Str(max=2000, optional=True)}), body or {})
    svc.review_run(_cid(user), parse(Uuid(), id_), user.id, b.get('notes'))
    return {'ok': True}


def approve(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    return {'ok': True, **svc.approve_run(_cid(user), parse(Uuid(), id_), user.id, user.roles)}


def lock(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    return svc.lock_run(_cid(user), parse(Uuid(), id_), user.id)


def publish(user: AuthUser, id_: str) -> dict:
    require_payroll(user)
    return {'published': svc.publish_run(_cid(user), parse(Uuid(), id_), user.id)}


# ---------------------------------------------------------------- outputs (plan §11.10)

def ssf_sheet(user: AuthUser, id_: str) -> tuple[str, str]:
    require_payroll(user)
    run = svc.get_run(_cid(user), parse(Uuid(), id_))
    return f"ssf-{run['id']}.csv", ssf_sheet_csv(run['id'])


def salary_sheet_for(user: AuthUser, id_: str, query: dict) -> tuple[str, dict]:
    """(format, sheet). JSON, ?format=csv for Excel, ?format=html to print. Audited like the TS route."""
    require_payroll(user)
    fmt = parse(Obj({'format': Enum(('json', 'csv', 'html'), default='json')}), query)['format']
    sheet = salary_sheet(_cid(user), parse(Uuid(), id_))
    if not sheet:
        raise NotFoundError('payroll run not found')
    if not sheet['rows'] and fmt != 'json':
        raise ConflictError('this run has no payslips yet; calculate it first')
    audit('salary_sheet', 'payroll_run', actor_id=user.id, row_id=sheet['run']['id'], after={'format': fmt})
    return fmt, sheet


# ---------------------------------------------------------------- payslips (employee self-service, plan §13.7)

def can_see_payslip(user: AuthUser, employee_id: str) -> bool:
    return has_module(user, 'payroll') or user.employee_id == str(employee_id)


def list_payslips(user: AuthUser, query: dict) -> dict:
    qy = parse(Obj({'employeeId': Uuid(optional=True), 'fiscalYearStart': Int(coerce=True, optional=True)}), query)
    employee_id = qy.get('employeeId') or user.employee_id
    if not employee_id:
        raise BadRequestError('employeeId required')
    assert_manages(user, employee_id)   # 404 outside the company, 403 for unrelated employees
    if not can_see_payslip(user, employee_id):
        raise ForbiddenError('payslips are visible to the employee and to HR only')
    only_published = not has_module(user, 'payroll')
    r = q("""SELECT ps.id, ps.run_id, r.run_type, p.bs_year, p.bs_month, ps.paid_days, ps.lop_days, ps.ot_hours, ps.gross, ps.tds, ps.total_deductions, ps.net,
                    ps.published_at, r.locked_at
               FROM payslip ps JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
              WHERE ps.employee_id = $1 AND r.company_id = $2 AND ($3::boolean = false OR ps.published_at IS NOT NULL)
                AND ($4::int IS NULL OR (CASE WHEN p.bs_month >= 4 THEN p.bs_year ELSE p.bs_year - 1 END) = $4::int)
              ORDER BY p.bs_year DESC, p.bs_month DESC, ps.created_at DESC""", [employee_id, _cid(user), only_published, qy.get('fiscalYearStart')])
    return {'payslips': [_strs(s, 'id', 'run_id') for s in r]}


def load_payslip(user: AuthUser, id_: str) -> tuple[dict, list[dict]]:
    pid = parse(Uuid(), id_)
    row = q1("""
        SELECT ps.*, e.emp_code, e.name_en, e.name_ne, e.designation, e.ssf_id, e.bank_account_last4, e.pan_enc, c.name_en AS company_en, c.name_ne AS company_ne,
               p.bs_year, p.bs_month, p.start_ad::text, p.end_ad::text, r.locked_at, r.company_id,
               (SELECT ou.name_en FROM employee_assignment a LEFT JOIN org_unit ou ON ou.id = a.org_unit_id WHERE a.employee_id = e.id AND a.valid @> p.end_ad LIMIT 1) AS department
          FROM payslip ps JOIN employee e ON e.id = ps.employee_id JOIN payroll_run r ON r.id = ps.run_id JOIN payroll_period p ON p.id = r.period_id
          JOIN company c ON c.id = r.company_id
         WHERE ps.id = $1""", [pid])
    if not row or str(row['company_id']) != user.company_id:
        raise NotFoundError('payslip not found')
    row = _strs(row, 'id', 'run_id', 'employee_id', 'company_id')
    if not can_see_payslip(user, row['employee_id']):
        raise ForbiddenError()
    if not has_module(user, 'payroll') and not row['published_at']:
        raise NotFoundError('payslip not found')
    lines = q("""SELECT component_code AS code, type, name_en, name_ne, amount, basis, sort_order FROM payslip_line WHERE payslip_id = $1
                  ORDER BY sort_order""", [pid])
    hold = q1("SELECT status, reason FROM payroll_hold WHERE run_id = $1 AND employee_id = $2 AND status IN ('held', 'releasing')",
              [row['run_id'], row['employee_id']])
    row['held'] = {'status': hold['status'], 'reason': hold['reason'] if has_module(user, 'payroll') else None} if hold else None
    return row, lines


def payslip_detail(user: AuthUser, id_: str) -> dict:
    row, lines = load_payslip(user, id_)
    rest = {k: v for k, v in row.items() if k not in ('pan_enc', 'inputs_snapshot')}
    return {'payslip': rest, 'lines': lines, 'tax': ((row.get('inputs_snapshot') or {}).get('result') or {}).get('tax')}


def payslip_html(user: AuthUser, id_: str) -> str:
    row, lines = load_payslip(user, id_)
    pol = q1("SELECT value FROM company_setting WHERE company_id = $1 AND key = 'payslip.policy'", [_cid(user)])
    numerals = 'devanagari' if ((pol or {}).get('value') or {}).get('numerals') == 'devanagari' else 'latin'
    pan = field_cipher().decrypt(bytes(row['pan_enc']), f"employee:{row['employee_id']}:pan") if row['pan_enc'] is not None else None
    ytd = row.get('ytd') or {}
    return render_payslip_html({
        'company': {'nameEn': row['company_en'], 'nameNe': row['company_ne']}, 'payslipId': row['id'], 'numerals': numerals,
        'employee': {'code': row['emp_code'], 'nameEn': row['name_en'], 'nameNe': row['name_ne'], 'designation': row['designation'],
                     'department': row['department'], 'panMasked': f'*****{pan[-4:]}' if pan else None,
                     'ssfMasked': f"*******{str(row['ssf_id'])[-4:]}" if row['ssf_id'] else None,
                     'bankMasked': f"****{row['bank_account_last4']}" if row['bank_account_last4'] else None},
        'period': {'bsYear': row['bs_year'], 'bsMonth': row['bs_month'], 'startAd': row['start_ad'], 'endAd': row['end_ad']},
        'paidDays': str(row['paid_days']), 'lopDays': str(row['lop_days']), 'otHours': str(row['ot_hours']),
        'lines': [{'code': ln['code'], 'type': ln['type'], 'nameEn': ln['name_en'], 'nameNe': ln['name_ne'], 'amount': str(ln['amount'])} for ln in lines],
        'gross': str(row['gross']), 'totalDeductions': str(row['total_deductions']), 'employerStatutory': str(row['employer_statutory']),
        'net': str(row['net']), 'ytd': {'gross': ytd.get('gross') or '0', 'tds': ytd.get('tds') or '0', 'statutory': ytd.get('statutory') or '0'},
        'explain': row.get('explain') or [], 'held': bool(row['held']),
    })


def has_nul(v) -> bool:
    if isinstance(v, str):
        return '\x00' in v
    if isinstance(v, dict):
        return any(has_nul(k) or has_nul(x_) for k, x_ in v.items())
    if isinstance(v, (list, tuple)):
        return any(has_nul(x_) for x_ in v)
    return False

