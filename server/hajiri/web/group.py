"""
Groups of companies in the web app: the company switcher, the group overview (one dashboard across the companies a
person may see), and the admin pages that add a sister company and give people of the group access to it. JSON
versions for Bearer clients are DRF views at the end. Rules live in hajiri.core.companies.
"""
from __future__ import annotations

from urllib.parse import parse_qs, urlsplit

from django import forms
from django.http import HttpResponse
from django.urls import Resolver404, resolve
from django.utils.http import url_has_allowed_host_and_scheme
from django.shortcuts import render
from django.views.decorators.http import require_GET, require_http_methods, require_POST
from drf_spectacular.utils import extend_schema
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from hajiri.core import auth, companies
from hajiri.core.api import IsSignedIn
from hajiri.core.auth import ROLES, has_role
from hajiri.core.errors import ForbiddenError
from hajiri.core.scoping import is_uuid
from hajiri.core.validation import validate

from . import nav
from .decorators import login_required, require_role
from .htmx import close_modal, hx_redirect, refresh, toast
from .nav import GROUP_ROLE_HELP, ROLE_HELP, ROLE_LABEL


# ---------------------------------------------------------------- forms

class CompanyForm(forms.Form):
    code = forms.RegexField(regex=r'^[A-Za-z0-9][A-Za-z0-9_-]{1,19}$', label='Code',
                            help_text='Short code, 2 to 20 letters or digits (used in device serials and exports).',
                            error_messages={'invalid': 'Use 2 to 20 letters, digits, - or _.'})
    name_en = forms.CharField(max_length=160, label='Name')
    name_ne = forms.CharField(max_length=160, required=False, label='Name in Nepali')


class AccessForm(forms.Form):
    email = forms.EmailField(max_length=200, label='Email of the person')
    roles = forms.MultipleChoiceField(choices=[(r, ROLE_LABEL[r]) for r in ROLES], label='Roles in this company',
                                      widget=forms.CheckboxSelectMultiple)
    employee_id = forms.UUIDField(required=False, label='Employee record in this company')


class GroupRoleForm(forms.Form):
    email = forms.EmailField(max_length=200, label='Email of the person')
    roles = forms.MultipleChoiceField(choices=[(r, ROLE_LABEL[r]) for r in auth.GROUP_ROLES], label='Group roles',
                                      widget=forms.CheckboxSelectMultiple)


class SwitchIn(serializers.Serializer):
    companyId = serializers.UUIDField()


class GroupRoleIn(serializers.Serializer):
    email = serializers.EmailField(max_length=200)
    roles = serializers.ListField(child=serializers.ChoiceField(auth.GROUP_ROLES), min_length=1)


# ---------------------------------------------------------------- switcher

def page_after_switch(request, user, next_url: str | None) -> str:
    """
    Where to land after switching company: the same page when it makes sense in the new company, else the first
    page of its hub, else the home page.

    - A page that shows a record (a UUID in its path or query) goes to its hub's first page: records belong to one
      company, so the id would not be found in the other one.
    - A page of a hub the person cannot see in the new company (other roles there, module switched off) goes home.
    - Pages outside the hubs (profile, security, the group overview) reload as they are.
    """
    if not next_url or not url_has_allowed_host_and_scheme(next_url, allowed_hosts={request.get_host()},
                                                           require_https=request.is_secure()):
        return '/'
    parts = urlsplit(next_url)
    path = parts.path or '/'
    if path.startswith('/_/') or path.startswith('/api/'):
        return '/'
    try:
        match = resolve(path)
    except Resolver404:
        return '/'
    hub_paths = [h for h in nav.HUBS if h.group != 'account' and nav._path_match(path, h.path)]
    navigation = nav.build(user, path, badges={})
    current = navigation.current
    shows_record = (any(is_uuid(v) for v in match.kwargs.values())
                    or any(is_uuid(v) for vs in parse_qs(parts.query).values() for v in vs))
    if hub_paths and current is None:
        return '/'
    if shows_record:
        return current.url if current else '/'
    return path + (f'?{parts.query}' if parts.query else '')


@require_POST
@login_required
def switch_company(request):
    """The company switcher: work in another of my companies, then reload the page in it (see page_after_switch)."""
    company_id = request.POST.get('company_id') or ''
    user = auth.switch_company(request.hajiri_user, request.session_token, company_id)
    name = next((c.name for c in companies.my_companies(user) if c.active), 'the company')
    target = page_after_switch(request, user, request.POST.get('next'))
    return toast(request, hx_redirect(request, target), f'Now working in {name}.')


@require_POST
@login_required
def default_company(request):
    """Account menu: start in this company at the next sign-in."""
    auth.set_default_company(request.hajiri_user, request.POST.get('company_id') or '')
    return toast(request, HttpResponse(status=204), 'You will start in this company when you sign in.')


# ---------------------------------------------------------------- group overview

@require_http_methods(['GET', 'POST'])
@require_role('admin')
def group_company_new(request):
    """Modal: add a company to the group (seeded with the standard leave types, approval flows and settings)."""
    form = CompanyForm(request.POST or None)
    if request.method == 'POST':
        if not form.is_valid():
            return render(request, 'web/partials/group_company_form.html', {'form': form}, status=422)
        c = companies.create_group_company(request.hajiri_user, form.cleaned_data)
        return toast(request, hx_redirect(request, '/group'), f"{c['name']} added to the group. Switch to it to set it up.")
    return render(request, 'web/partials/group_company_form.html', {'form': form})


@require_http_methods(['GET', 'POST'])
@require_role('admin')
def group_access_new(request):
    """Modal: give a person of the group access to the company I am working in."""
    form = AccessForm(request.POST or None)
    ctx = {'form': form, 'role_help': ROLE_HELP}
    if request.method == 'POST':
        if not form.is_valid():
            return render(request, 'web/partials/group_access_form.html', ctx, status=422)
        d = form.cleaned_data
        companies.grant_access(request.hajiri_user, {'email': d['email'], 'roles': d['roles'],
                                                     'employee_id': str(d['employee_id']) if d['employee_id'] else None})
        resp = HttpResponse(status=204)
        close_modal(resp)
        refresh(resp, 'group-members')
        refresh(resp, 'users')
        return toast(request, resp, 'Access given.')
    return render(request, 'web/partials/group_access_form.html', ctx)


@require_GET
@require_role('admin')
def group_members(request):
    return render(request, 'web/partials/group_members.html', {'members': companies.list_memberships(request.hajiri_user)})


@require_POST
@require_role('admin')
def group_access_revoke(request, user_id):
    companies.revoke_access(request.hajiri_user, str(user_id))
    return toast(request, refresh(HttpResponse(status=204), 'group-members'), 'Access removed.')


def _require_group_admin(request):
    if not companies.is_group_admin(request.hajiri_user):
        raise ForbiddenError('requires role group_admin')


@require_http_methods(['GET', 'POST'])
@login_required
def group_role_new(request):
    """Modal: give a person of the group group roles (group admins only)."""
    _require_group_admin(request)
    form = GroupRoleForm(request.POST or None)
    ctx = {'form': form, 'role_help': GROUP_ROLE_HELP}
    if request.method == 'POST':
        if not form.is_valid():
            return render(request, 'web/partials/group_role_form.html', ctx, status=422)
        companies.grant_group_role(request.hajiri_user, form.cleaned_data)
        resp = HttpResponse(status=204)
        close_modal(resp)
        refresh(resp, 'group-roles')
        return toast(request, resp, 'Group roles saved.')
    return render(request, 'web/partials/group_role_form.html', ctx)


@require_GET
@login_required
def group_roles(request):
    _require_group_admin(request)
    return render(request, 'web/partials/group_roles.html', {'group_roles': companies.list_group_roles(request.hajiri_user)})


@require_POST
@login_required
def group_role_revoke(request, user_id):
    _require_group_admin(request)
    companies.revoke_group_role(request.hajiri_user, str(user_id))
    return toast(request, refresh(HttpResponse(status=204), 'group-roles'), 'Group roles removed.')


# ---------------------------------------------------------------- JSON (DRF)

@extend_schema(responses=dict)
@api_view(['GET'])
@permission_classes([IsSignedIn])
def api_my_companies(request):
    """GET /api/v1/me/companies: the companies I can work in, with my roles there and which one is active."""
    return Response({'companies': [
        {'id': c.id, 'code': c.code, 'name': c.name, 'groupId': c.group_id, 'roles': list(c.roles),
         'employeeId': c.employee_id, 'home': c.home, 'active': c.active} for c in companies.my_companies(request.user)],
        'groupId': request.user.group_id, 'groupRoles': list(request.user.group_roles)})


@extend_schema(request=SwitchIn, responses=dict)
@api_view(['POST'])
@permission_classes([IsSignedIn])
def api_switch_company(request):
    """POST /api/v1/me/companies/switch {companyId}: this session works in that company from now on."""
    data = validate(SwitchIn, request.data)
    user = auth.switch_company(request.user, request.auth, str(data['companyId']))
    return Response({'companyId': user.company_id, 'roles': list(user.roles), 'employeeId': user.employee_id})


@extend_schema(responses=dict)
@api_view(['GET'])
@permission_classes([IsSignedIn])
def api_group_overview(request):
    """GET /api/v1/group/overview: headline figures of every company of the group I may see."""
    return Response(companies.group_overview(request.user))



@extend_schema(request=GroupRoleIn, responses=dict)
@api_view(['GET', 'POST'])
@permission_classes([IsSignedIn])
def api_group_roles(request):
    """GET /api/v1/group/roles: people with a group role in my group. POST {email, roles}: give or replace them."""
    if request.method == 'GET':
        return Response({'items': companies.list_group_roles(request.user)})
    data = validate(GroupRoleIn, request.data)
    return Response(companies.grant_group_role(request.user, data), status=201)


@extend_schema(responses={204: None})
@api_view(['DELETE'])
@permission_classes([IsSignedIn])
def api_group_role_revoke(request, user_id):
    """DELETE /api/v1/group/roles/<userId>: remove all of a person's group roles."""
    companies.revoke_group_role(request.user, str(user_id))
    return Response(status=204)
