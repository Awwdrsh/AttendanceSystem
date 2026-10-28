"""
Group administration pages, tabs of the Group hub: Companies, Access, Transfers, Policies (/group/...). Fragments
and form posts live under /_/group-admin/... . Forms read the posts; services hold the rules and the audit.
"""
from __future__ import annotations

from functools import wraps

from django.http import HttpResponse
from django.shortcuts import render
from django.urls import reverse
from django.views.decorators.http import require_GET, require_http_methods, require_POST

from hajiri.core.errors import ForbiddenError, ValidationError
from hajiri.web.decorators import login_required
from hajiri.web.htmx import close_modal, hx_redirect, refresh, toast
from hajiri.web.nav import ROLE_HELP

from . import forms, services


def group_admin_required(view):
    """Signed in (password changed, two-step set up) and group admin of the active company's group."""
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not services.can_administer(request.hajiri_user):
            raise ForbiddenError('requires role group_admin')
        return view(request, *args, **kwargs)
    return wrapped


def group_hr_required(view):
    """Group HR or group admin."""
    @login_required
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not services.can_transfer(request.hajiri_user):
            raise ForbiddenError('requires role group_hr')
        return view(request, *args, **kwargs)
    return wrapped


def _saved(request, message: str, target: str) -> HttpResponse:
    resp = HttpResponse(status=204)
    close_modal(resp)
    refresh(resp, target)
    return toast(request, resp, message)


# ---------------------------------------------------------------- companies

@require_GET
@group_admin_required
def companies(request):
    user = request.hajiri_user
    return render(request, 'group_admin/companies.html', {
        'companies': services.list_companies(user), 'can_attach': bool(services.attachable_companies(user))})


@require_GET
@group_admin_required
def companies_table(request):
    return render(request, 'group_admin/partials/companies_table.html', {'companies': services.list_companies(request.hajiri_user)})


@require_http_methods(['GET', 'POST'])
@group_admin_required
def company_new(request):
    form = forms.CompanyForm(request.POST or None, request.FILES or None)
    if request.method == 'POST':
        if not form.is_valid():
            return render(request, 'group_admin/partials/company_form.html', {'form': form}, status=422)
        c = services.create_company(request.hajiri_user, form.body())
        return _saved(request, f"{c['nameEn']} added to the group with the standard leave types, approval flows and settings.",
                      'group-companies')
    return render(request, 'group_admin/partials/company_form.html', {'form': form})


@require_http_methods(['GET', 'POST'])
@group_admin_required
def company_edit(request, company_id):
    user = request.hajiri_user
    current = services.get_company(user, company_id)     # 404 for a company outside the group, before any form
    if request.method == 'POST':
        form = forms.CompanyEditForm(request.POST, request.FILES or None)
        if not form.is_valid():
            return render(request, 'group_admin/partials/company_form.html', {'form': form, 'company': current}, status=422)
        services.update_company(user, company_id, form.body())
        return _saved(request, 'Company saved.', 'group-companies')
    form = forms.CompanyEditForm(initial={'name_en': current['nameEn'], 'name_ne': current['nameNe'], 'pan': current['pan'],
                                          'address': current['address']})
    return render(request, 'group_admin/partials/company_form.html', {'form': form, 'company': current})


@require_http_methods(['GET', 'POST'])
@group_admin_required
def company_attach(request):
    user = request.hajiri_user
    form = forms.AttachForm(user, request.POST or None)
    if request.method == 'POST':
        if not form.is_valid():
            return render(request, 'group_admin/partials/attach_form.html', {'form': form}, status=422)
        c = services.attach_company(user, form.body())
        return _saved(request, f"{c['name']} is now part of the group.", 'group-companies')
    return render(request, 'group_admin/partials/attach_form.html', {'form': form})


# ---------------------------------------------------------------- access

def _access_ctx(request) -> dict:
    user = request.hajiri_user
    company = request.GET.get('company') or None
    return {'rows': services.list_access(user, company), 'company': company,
            'company_choices': services.company_choices(user)}


@require_GET
@group_admin_required
def access(request):
    return render(request, 'group_admin/access.html', _access_ctx(request))


@require_GET
@group_admin_required
def access_table(request):
    return render(request, 'group_admin/partials/access_table.html', _access_ctx(request))


@require_GET
@group_admin_required
def access_employees(request):
    """The employee select of the access forms, for the chosen company."""
    company = request.GET.get('company_id') or ''
    keep = request.GET.get('employee_id') or None
    employees = services.unlinked_employees(request.hajiri_user, company, keep) if company else []
    return render(request, 'group_admin/partials/employee_select.html', {'employees': employees, 'selected': keep})


def _access_form(request, form_cls, template, message):
    user = request.hajiri_user
    initial = {k: request.GET[k] for k in ('company_id', 'email') if request.GET.get(k)}
    if request.GET.get('roles'):
        initial['roles'] = request.GET.getlist('roles')
    form = form_cls(user, request.POST or None, initial=initial)
    company = (request.POST.get('company_id') if request.method == 'POST' else initial.get('company_id')) \
        or (form.fields['company_id'].choices[0][0] if form.fields['company_id'].choices else '')
    selected = request.POST.get('employee_id') or request.GET.get('employee_id') or None
    ctx = {'form': form, 'role_help': ROLE_HELP, 'company': company, 'selected': selected,
           'employees': services.unlinked_employees(user, company, selected) if company else []}
    if request.method == 'POST':
        if not form.is_valid():
            return render(request, template, ctx, status=422)
        fn = services.invite_user if form_cls is forms.InviteForm else services.grant_access
        fn(user, form.body())
        return _saved(request, message, 'group-access')
    return render(request, template, ctx)


@require_http_methods(['GET', 'POST'])
@group_admin_required
def access_new(request):
    """Modal: give or change a person's access to a company of the group."""
    return _access_form(request, forms.AccessForm, 'group_admin/partials/access_form.html', 'Access saved.')


@require_http_methods(['GET', 'POST'])
@group_admin_required
def access_invite(request):
    """Modal: a new person whose home is a company of the group."""
    return _access_form(request, forms.InviteForm, 'group_admin/partials/invite_form.html',
                        'Account created. Share the temporary password with them; they change it at the first sign-in.')


@require_POST
@group_admin_required
def access_revoke(request, user_id, company_id):
    services.revoke_access(request.hajiri_user, user_id, company_id)
    return toast(request, refresh(HttpResponse(status=204), 'group-access'), 'Access removed.')


# ---------------------------------------------------------------- transfers

def _transfer_ctx(request, form=None, target=None) -> dict:
    user = request.hajiri_user
    return {'form': form or forms.TransferForm(user), 'transfers': services.list_transfers(user),
            'target': target, 'rules': services.transfer_rules(user), 'can_set_rules': services.can_administer(user),
            'can_transfer_here': 'hr' in user.roles or 'admin' in user.roles}


@require_GET
@group_hr_required
def transfers(request):
    return render(request, 'group_admin/transfers.html', _transfer_ctx(request))


@require_GET
@group_hr_required
def transfer_target(request):
    """The target company's branches, departments, managers and shifts for the transfer form."""
    company = request.GET.get('to_company_id') or ''
    target = services.target_options(request.hajiri_user, company) if company else None
    return render(request, 'group_admin/partials/transfer_target.html', {'target': target})


@require_POST
@group_hr_required
def transfer_new(request):
    user = request.hajiri_user
    form = forms.TransferForm(user, request.POST)
    if not form.is_valid():
        company = request.POST.get('to_company_id')
        target = services.target_options(user, company) if company and company in dict(form.fields['to_company_id'].choices) else None
        return render(request, 'group_admin/partials/transfer_form.html', _transfer_ctx(request, form, target), status=422)
    r = services.transfer_employee(user, form.body())
    note = f" Salary not carried: {r['salaryNote']}." if r.get('salaryNote') else ''
    return toast(request, hx_redirect(request, reverse('group_admin:transfers')),
                 f"{r['name']} moves to {r['toCompany']}. The exit and final settlement checklist started in {r['fromCompany']}.{note}")


@require_POST
@group_admin_required
def transfer_rule(request):
    user = request.hajiri_user
    form = forms.TransferRuleForm(user, request.POST)
    if not form.is_valid():
        raise_form(form)
    services.set_transfer_rule(user, form.body())
    return toast(request, HttpResponse(status=204), 'Transfer rule saved.')


def raise_form(form):
    """A form error as the ValidationError the services raise (a red toast naming the field)."""
    raise ValidationError([{'path': k if k != '__all__' else '', 'message': str(v[0])} for k, v in form.errors.items()])


# ---------------------------------------------------------------- policies

@require_GET
@group_admin_required
def policies(request):
    user = request.hajiri_user
    form = forms.PolicyCopyForm(user, initial={'source_id': user.company_id, 'on_conflict': 'skip'})
    return render(request, 'group_admin/policies.html', {'form': form})


def _policy_post(request, run):
    """The choices of the copy form; a missing choice is a validation toast (the form stays as it is)."""
    user = request.hajiri_user
    form = forms.PolicyCopyForm(user, request.POST)
    if not form.is_valid():
        raise_form(form)
    return run(user, form.body())


@require_POST
@group_admin_required
def policies_preview(request):
    result = _policy_post(request, services.preview_policies)
    return render(request, 'group_admin/partials/policy_preview.html', {'preview': result, 'applied': False})


@require_POST
@group_admin_required
def policies_apply(request):
    result = _policy_post(request, services.copy_policies)
    t = result['totals']
    return toast(request, render(request, 'group_admin/partials/policy_preview.html', {'preview': result, 'applied': True}),
                 f"Copied: {t['create']} created, {t['overwrite']} overwritten.")
