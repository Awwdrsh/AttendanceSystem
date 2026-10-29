"""
JSON endpoints of the group views (DRF), for the React app (session cookie, Origin-checked like every JSON endpoint
it calls) and Bearer clients. Access rules are the services' (group_scope): anyone outside group views gets 403, a
company outside the scope 404.

    GET  /api/v1/me/companies              the companies I can work in, my roles there, my group roles and what the
                                           Group hub offers me (takes over hajiri.web.group's: same fields plus flags)
    POST /api/v1/me/companies/switch       {companyId}: this session works in that company from now on
    GET  /api/v1/group/dashboard[?refresh=1]
    GET  /api/v1/group/people              the directory across companies, with breakdowns (?format=csv,
                                           ?format=csv&export=breakdowns)
    GET  /api/v1/group/reports             the report catalogue, the companies in scope and the period choices
    GET  /api/v1/group/reports/<kind>      one consolidated report as {title, period, columns, rows} (?format=csv)
    GET/POST /api/v1/group/roles, DELETE /api/v1/group/roles/<userId>   group roles (group admins)
    GET/PUT  /api/v1/group/pay-policy      the active company's admin: hide its pay figures from executives
"""
from __future__ import annotations

from django.db.models import Q
from drf_spectacular.utils import OpenApiParameter, extend_schema
from rest_framework.response import Response
from rest_framework.views import APIView

from engines.bs_calendar import BS_MONTH_NAMES_EN, MAX_YEAR, MIN_YEAR
from hajiri.core import auth, companies
from hajiri.core.api import IsSignedIn, SameOriginAuthentication
from hajiri.core.auth import company_id_of, has_role
from hajiri.core.bsdate import bs_label
from hajiri.core.clock import npt_today
from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.core.validation import validate
from hajiri.group_admin import common as admin_common
from hajiri.reports.csv import csv_response, table_response

from . import services
from .filters import DirectoryFilter
from .serializers import REPORT_KINDS, DirectoryQueryIn, GroupRoleIn, PayPolicyIn, ReportQueryIn, SwitchIn

AUTH = [SameOriginAuthentication]


class _View(APIView):
    authentication_classes = AUTH
    permission_classes = [IsSignedIn]


# ---------------------------------------------------------------- my companies and the switcher

def my_companies_json(user) -> dict:
    cs = companies.my_companies(user)
    active = next((c for c in cs if c.active), None)
    return {
        'companies': [{'id': c.id, 'code': c.code, 'name': c.name, 'groupId': c.group_id, 'roles': list(c.roles),
                       'employeeId': c.employee_id, 'home': c.home, 'active': c.active} for c in cs],
        'groupId': user.group_id, 'groupName': active.group_name if active else None,
        'groupRoles': list(user.group_roles),
        # What the Group hub offers this person (the rules each group endpoint applies).
        'canViewGroup': services.can_view(user),
        'canAdministerGroup': admin_common.can_administer(user),
        'canTransfer': admin_common.can_transfer(user),
        # The admin of a standalone company may start a group by adding a sister company.
        'canStartGroup': not user.group_id and has_role(user, 'admin'),
    }


class MyCompaniesView(_View):
    @extend_schema(operation_id='me_companies', responses=dict)
    def get(self, request):
        return Response(my_companies_json(request.user))


class SwitchCompanyView(_View):
    @extend_schema(operation_id='me_companies_switch', request=SwitchIn, responses=dict)
    def post(self, request):
        data = validate(SwitchIn, request.data)
        user = auth.switch_company(request.user, request.auth, str(data['companyId']))
        return Response({'companyId': user.company_id, 'roles': list(user.roles), 'employeeId': user.employee_id})


# ---------------------------------------------------------------- group roles

class GroupRolesView(_View):
    @extend_schema(operation_id='group_roles_list', responses=dict)
    def get(self, request):
        return Response({'items': companies.list_group_roles(request.user)})

    @extend_schema(operation_id='group_roles_grant', request=GroupRoleIn, responses=dict)
    def post(self, request):
        data = validate(GroupRoleIn, request.data)
        return Response(companies.grant_group_role(request.user, data), status=201)


class GroupRoleRevokeView(_View):
    @extend_schema(operation_id='group_roles_revoke', responses={204: None})
    def delete(self, request, user_id):
        companies.revoke_group_role(request.user, str(user_id))
        return Response(status=204)


# ---------------------------------------------------------------- pay policy (the active company's own setting)

class PayPolicyView(_View):
    """Whether executives of the group see this company's pay figures in group views. Admins of the active company."""

    @extend_schema(operation_id='group_pay_policy', responses=dict)
    def get(self, request):
        if not has_role(request.user, 'admin'):
            raise ForbiddenError('requires role admin')
        return Response(services.pay_policy(company_id_of(request.user)))

    @extend_schema(operation_id='group_pay_policy_set', request=PayPolicyIn, responses=dict)
    def put(self, request):
        data = validate(PayPolicyIn, request.data)
        return Response(services.set_pay_policy(request.user, data[services.HIDE_PAY]))


# ---------------------------------------------------------------- dashboard

class DashboardView(_View):
    """GET /api/v1/group/dashboard: the consolidated dashboard (per company and totals)."""

    @extend_schema(responses=dict)
    def get(self, request):
        return Response(services.dashboard(request.user, fresh=request.query_params.get('refresh') == '1'))


# ---------------------------------------------------------------- directory

SORTS = {'name': ('name_en', 'emp_code'), 'code': ('emp_code',), 'company': ('company_name', 'name_en'),
         'joined': ('join_date', 'name_en'), '-joined': ('-join_date', 'name_en')}

PEOPLE_CSV = [
    {'key': 'empCode', 'header': 'Code'}, {'key': 'name', 'header': 'Name'}, {'key': 'companyName', 'header': 'Company'},
    {'key': 'designation', 'header': 'Designation'}, {'key': 'department', 'header': 'Department'},
    {'key': 'branch', 'header': 'Branch'}, {'key': 'employmentTypeLabel', 'header': 'Employment type'},
    {'key': 'genderLabel', 'header': 'Gender'}, {'key': 'joinedBs', 'header': 'Joined'},
    {'key': 'status', 'header': 'Status'}, {'key': 'email', 'header': 'Email'},
]
BREAKDOWN_CSV = [{'key': 'dimension', 'header': 'Breakdown'}, {'key': 'value', 'header': 'Value'},
                 {'key': 'company', 'header': 'Company'}, {'key': 'headcount', 'header': 'Headcount'}]
FIELDS = ('id', 'emp_code', 'name_en', 'company_id', 'company_name', 'company_code', 'designation', 'department',
          'branch_name', 'employment_type', 'gender', 'join_date', 'status', 'email')
# The directory filters' names in DirectoryFilter, by query parameter.
FILTERS = (('companyId', 'company'), ('gender', 'gender'), ('employmentType', 'employment_type'),
           ('branch', 'branch'), ('department', 'department'))


def _person(r: dict) -> dict:
    return {'id': str(r['id']), 'empCode': r['emp_code'], 'name': r['name_en'], 'companyId': str(r['company_id']),
            'companyName': r['company_name'], 'companyCode': r['company_code'], 'designation': r['designation'],
            'department': r['department'], 'branch': r['branch_name'], 'employmentType': r['employment_type'],
            'employmentTypeLabel': services.EMPLOYMENT_LABEL.get(r['employment_type'], r['employment_type'] or ''),
            'gender': r['gender'], 'genderLabel': services.GENDER_LABEL.get(r['gender'], r['gender'] or ''),
            'joinDate': r['join_date'], 'joinedBs': bs_label(r['join_date']) if r['join_date'] else '',
            'status': r['status'], 'email': r['email']}


class PeopleView(_View):
    """GET /api/v1/group/people: everyone in the companies of the group I may see, filtered and paged, with headcount
    by company, branch, department, gender and employment type of the filtered set."""

    @extend_schema(parameters=[DirectoryQueryIn], responses=dict)
    def get(self, request):
        q = validate(DirectoryQueryIn, request.query_params.dict())
        user = request.user
        scope = services.group_scope(user)
        if q.get('companyId'):
            services.narrow(scope, str(q['companyId']))         # outside the scope: company not found
        data = {'status': q['status'], **{name: str(q[key]) for key, name in FILTERS if q.get(key)}}
        qs = DirectoryFilter(data, services.directory(user), scope=scope).qs
        if (s := (q.get('q') or '').strip()):
            qs = qs.filter(Q(name_en__icontains=s) | Q(emp_code__icontains=s) | Q(email__icontains=s))
        if q['format'] == 'csv' and q['export'] == 'breakdowns':
            rows = [{'dimension': services.DIMENSIONS[dim][0], 'value': r['label'], 'company': r['company'],
                     'headcount': r['value']} for dim, rs in services.breakdowns(qs).items() for r in rs]
            return csv_response('group-headcount-breakdowns', BREAKDOWN_CSV, rows)
        qs = qs.order_by(*SORTS[q['sort']])
        if q['format'] == 'csv':
            return csv_response('group-people', PEOPLE_CSV, [_person(r) for r in qs.values(*FIELDS)])
        start = (q['page'] - 1) * q['pageSize']
        items = [_person(r) for r in qs.values(*FIELDS)[start:start + q['pageSize']]]
        b = services.breakdowns(qs)
        breakdowns = [{'key': dim, 'title': title, 'rows': b[dim],
                       'totals': ([{'label': r['label'], 'value': r['value']} for r in b[dim]] if dim == 'company'
                                  else services.breakdown_totals(b[dim]))}
                      for dim, (title, _) in services.DIMENSIONS.items()]
        return Response({'items': items, 'total': qs.count(), 'page': q['page'], 'pageSize': q['pageSize'],
                         'breakdowns': breakdowns,
                         'companies': [{'id': c.id, 'code': c.code, 'name': c.name} for c in scope.companies]})


# ---------------------------------------------------------------- reports

def fiscal_years(today) -> list[dict]:
    """This fiscal year and the ten before it (within the calendar table)."""
    cur = min(services.current_fiscal_start(today), MAX_YEAR - 1)
    return [{'value': y, 'label': services.fiscal_label(y)} for y in range(cur, max(MIN_YEAR, cur - 10) - 1, -1)]


class ReportCatalogView(_View):
    """GET /api/v1/group/reports: what the reports page offers (reports, companies in scope, period choices)."""

    @extend_schema(operation_id='group_reports_catalog', responses=dict)
    def get(self, request):
        scope = services.group_scope(request.user)
        today = npt_today()
        cur = services.current_month(today)
        return Response({
            'reports': [{'key': k, 'title': v[0], 'description': v[1]} for k, v in services.REPORTS.items()],
            'companies': [{'id': c.id, 'code': c.code, 'name': c.name} for c in scope.companies],
            'current': {'bsYear': cur.year, 'bsMonth': cur.month, 'fy': services.current_fiscal_start(today)},
            'months': [{'value': n, 'label': label} for n, label in enumerate(BS_MONTH_NAMES_EN, start=1)],
            'fiscalYears': fiscal_years(today),
        })


class ReportView(_View):
    """GET /api/v1/group/reports/<kind>: one consolidated report as {title, period, columns, rows}; ?format=csv
    downloads it."""

    @extend_schema(parameters=[OpenApiParameter('kind', str, OpenApiParameter.PATH, enum=list(REPORT_KINDS))],
                   responses=dict)
    def get(self, request, kind):
        if kind not in REPORT_KINDS:
            raise NotFoundError('report not found')
        q = validate(ReportQueryIn, request.query_params.dict())
        period = services.period_of(q['period'], bs_year=q.get('bsYear'), bs_month=q.get('bsMonth'), fy=q.get('fy'))
        cid = str(q['companyId']) if q.get('companyId') else None
        table = services.report(request.user, kind, period, cid)
        if request.query_params.get('format') == 'csv':
            months = period.months
            table.filename = f'{table.filename}-{months[0].key}' + (f'-to-{months[-1].key}' if len(months) > 1 else '')
            return table_response(table)
        return Response(table.to_dict())
