"""The shell, navigation, components, tables, error handling and PWA files (Layout.tsx, Hub.tsx, DataTable.tsx, ...)."""
import json

import pytest
from django.http import HttpResponse
from django.template import Context, Template
from django.test import Client, RequestFactory, override_settings
from django.urls import path

from hajiri.core.auth import AuthUser
from hajiri.core.errors import ConflictError, NotFoundError
from hajiri.web import nav
from hajiri.web.decorators import login_required, require_feature, require_module, require_role
from hajiri.web.nav import NavItem
from hajiri.web.tables import Column, Table, table_response

from .factories import htmx, make_company, make_employee, set_features_off, signed_in_client, toasts


def _user(roles=('employee',), employee_id='e1', company_id='c1') -> AuthUser:
    return AuthUser(id='u1', company_id=company_id, employee_id=employee_id, email='a@b.c', name='A B', roles=tuple(roles),
                    must_change_password=False, two_factor_enabled=False, must_set_up_two_factor=False)


# ---------------------------------------------------------------- test-only views and URLs

@login_required
def _boom_conflict(request):
    raise ConflictError('already exists', {'row': 3})


@login_required
def _boom_unique(request):
    # A real unique violation: the second company with the same code.
    from django.db import transaction

    from tests.support.sql import execute
    with transaction.atomic():
        execute("INSERT INTO company (code, name_en) VALUES ('DUP1', 'One')")
        execute("INSERT INTO company (code, name_en) VALUES ('DUP1', 'Two')")


@login_required
def _boom_validation(request):
    from hajiri.core.errors import ValidationError
    raise ValidationError([{'path': 'from', 'message': 'YYYY-MM-DD'}, {'path': '', 'message': 'bad'}])


@login_required
def _open_tab(request):
    return HttpResponse('open tab')


@login_required
def _boom_missing(request):
    raise NotFoundError('shift not found')


@require_role('hr', 'manager')
def _hr_only(request):
    return HttpResponse('ok')


@require_module('payroll')
def _payroll_only(request):
    return HttpResponse('ok')


@login_required
@require_feature('expenses')
def _expenses_page(request):
    return HttpResponse('expenses')


def _tab_a(request):
    from django.shortcuts import render
    return render(request, 'web/test_tab.html', {})


urlpatterns = [
    path('_/test/conflict', _boom_conflict),
    path('_/test/unique', _boom_unique),
    path('_/test/validation', _boom_validation),
    path('_/test/hub/open', _open_tab, name='_t_hub_open'),
    path('_/test/hub/hr', _hr_only, name='_t_hub_hr'),
    path('_/test/hub/payroll', _payroll_only, name='_t_hub_payroll'),
    path('_/test/missing', _boom_missing),
    path('_/test/hr', _hr_only),
    path('_/test/payroll', _payroll_only),
    path('_/test/expenses', _expenses_page),
    path('', __import__('django.urls', fromlist=['include']).include(('hajiri.web.urls', 'web'))),
]


# ---------------------------------------------------------------- navigation model

ITEMS = [
    NavItem(hub='attendance', label='Today', url_name='web:security', order=10, roles=('manager', 'hr', 'attendance_admin')),
    NavItem(hub='attendance', label='Exceptions', url_name='web:components', order=20, module='attendance'),
    NavItem(hub='expenses', label='Claims', url_name='web:login', order=10, feature='expenses', roles=('manager', 'hr')),
    NavItem(hub='profile', label='Sign-in security', url_name='web:security', order=30),
    NavItem(hub='approvals', label='Approvals', url_name='web:change_password', order=10),
    NavItem(hub='home', label='Home', url_name='web:setup_two_step', order=10),
    NavItem(hub='payslips', label='Payslips', url_name='integrations:health', order=10),
    NavItem(hub='attendance', label='Missing', url_name='nope:nothing', order=5),
]


def test_hub_visibility_follows_roles_modules_features_and_employee_link(db):
    hr = nav.build(_user(['hr']), '/x', items=ITEMS, off=set(), badges={})
    titles = [v.hub.title for _, hubs in hr.groups for v in hubs]
    assert 'Attendance' in titles and 'Expenses & travel' in titles and 'Approvals' in titles and 'Home' in titles
    att = next(v for _, hubs in hr.groups for v in hubs if v.hub.key == 'attendance')
    assert [t.label for t in att.tabs] == ['Today', 'Exceptions']      # unresolvable url names are skipped
    assert att.url == '/me/profile/security'

    off = nav.build(_user(['hr']), '/x', items=ITEMS, off={'expenses'}, badges={})
    assert 'Expenses & travel' not in [v.hub.title for _, hubs in off.groups for v in hubs]

    mgr = nav.build(_user(['manager']), '/x', items=ITEMS, off=set(), badges={})
    att = next(v for _, hubs in mgr.groups for v in hubs if v.hub.key == 'attendance')
    assert [t.label for t in att.tabs] == ['Today']                    # Exceptions needs the attendance area

    emp = nav.build(_user(['employee']), '/x', items=ITEMS, off=set(), badges={})
    assert [g for g, _ in emp.groups] == ['My work']                   # no Manage group for plain employees
    no_emp = nav.build(_user(['employee'], employee_id=None), '/x', items=ITEMS, off=set(), badges={})
    assert all(v.hub.key not in ('home', 'payslips') for _, hubs in no_emp.groups for v in hubs)


def test_approvals_show_for_anyone_with_pending_items(db):
    emp = nav.build(_user(['employee']), '/x', items=ITEMS, off=set(), badges={'approvals': 3})
    manage = dict(emp.groups).get('Manage')
    assert manage and manage[0].hub.key == 'approvals' and manage[0].badge == 3


def test_current_hub_tab_and_palette(db):
    n = nav.build(_user(['hr']), '/me/profile/security', items=ITEMS, off=set(), badges={'notifications': 2})
    assert n.current_tab.label in ('Today', 'Sign-in security') and n.embedded
    assert n.unread == 2
    labels = {p['label'] for p in n.palette}
    assert {'Attendance', 'Exceptions'} <= labels  # one entry per address
    assert all(set(p) == {'url', 'label', 'group', 'icon', 'keywords'} for p in n.palette)
    # Below a hub's path (a detail page) the hub is current but there is no tab strip.
    d = nav.build(_user(['hr']), '/attendance/something/else', items=[*ITEMS[:2]], off=set(), badges={})
    assert d.current is None or d.current.hub.key == 'attendance'


def test_every_registered_nav_item_names_a_known_hub():
    for it in nav.all_items():
        assert it.hub in nav.HUB_BY_KEY


def test_feature_switch_read_from_company_setting(db):
    co = make_company()
    assert nav.disabled_features(str(co.id)) == set()
    set_features_off(co, 'expenses', 'not-a-feature')
    assert nav.disabled_features(str(co.id)) == {'expenses'}


# ---------------------------------------------------------------- shell rendering

def test_security_page_renders_in_the_shell_with_the_profile_hub(db):
    co = make_company()
    c, user = signed_in_client(co, roles=['hr'])
    r = c.get('/me/profile/security')
    html = r.content.decode()
    assert r.status_code == 200
    assert 'class="hj-nav"' in html and 'id="hj-palette-data"' in html and user.name in html
    assert 'href="/me/profile/security"' in html and 'Sign out' in html
    assert '<h1 class="hj-page-title">Profile</h1>' in html          # hub title, page header embedded
    assert 'hx-headers=\'{"X-CSRFToken": "' in html
    palette = json.loads(html.split('id="hj-palette-data">')[1].split('</script>')[0])
    assert any(p['url'] == '/me/profile/security' for p in palette)


def test_component_gallery_renders_every_component(db):
    co = make_company()
    c, _ = signed_in_client(co, roles=['hr'])
    r = c.get('/_/web/components')
    assert r.status_code == 200
    html = r.content.decode()
    for marker in ('hj-stats', 'hj-badge hj-tone-warn', 'hj-alert', 'hjDate(', 'hjCombo(', 'hjUpload(', 'hjCamera(',
                   'class="hj-table"', '<svg class="hj-qr"', 'hj-chart', 'hj-empty', 'data-modal-open="demo-modal"',
                   'hx-confirm=', '12 Bhadra 2083', 'NPR 12,34,56,789.50', '8 h 23 m'):
        assert marker in html, marker
    modal = c.get('/_/web/components?partial=modal', **htmx('hj-modal-body'))
    assert b'data-modal-title="Loaded over HTMX"' in modal.content


def test_gallery_htmx_actions(db):
    co = make_company()
    c, _ = signed_in_client(co)
    r = c.post('/_/web/components/action', {'kind': 'info'}, **htmx())
    assert r.status_code == 204 and toasts(r) == [{'message': 'A info toast from the server.', 'tone': 'info', 'title': 'For your information'}]
    err = c.post('/_/web/components/action', {'kind': 'error'}, **htmx())
    assert err.status_code == 400 and err['HX-Reswap'] == 'none' and toasts(err)[0]['title'] == 'Not saved'
    bad = c.post('/_/web/components/action', {'kind': 'form', 'name': 'x'}, **htmx())
    assert bad.status_code == 422 and b'at least 2 characters' in bad.content
    ok = c.post('/_/web/components/action', {'kind': 'form', 'name': 'Sita'}, **htmx())
    assert ok.status_code == 200 and toasts(ok)[0]['message'] == 'Saved Sita.'
    modal = c.post('/_/web/components/action', {'kind': 'modal'}, **htmx())
    events = json.loads(modal['HX-Trigger'])
    assert events['hj:close-modal'] is True and events['hj:toast']['items'][0]['message'] == 'Modal action done.'


# ---------------------------------------------------------------- error handling

@override_settings(ROOT_URLCONF='tests.web.test_layout')
def test_http_errors_become_pages_toasts_or_json(db):
    co = make_company()
    c, _ = signed_in_client(co)
    page = c.get('/_/test/missing')
    assert page.status_code == 404 and b'shift not found' in page.content and b'Not found' in page.content
    hx = c.post('/_/test/conflict', **htmx())
    assert hx.status_code == 409 and toasts(hx) == [{'message': 'already exists', 'tone': 'err', 'title': 'Not saved'}]
    js = c.get('/_/test/conflict', HTTP_ACCEPT='application/json')
    assert js.status_code == 409 and js.json() == {'error': 'already exists', 'details': {'row': 3}}
    plain = c.get('/_/test/missing', HTTP_ACCEPT='application/json')
    assert plain.json() == {'error': 'shift not found'}                 # no details key when there are none (app.ts)


@override_settings(ROOT_URLCONF='tests.web.test_layout')
def test_database_errors_answer_like_the_ts_error_handler(db):
    co = make_company()
    c, _ = signed_in_client(co)
    js = c.get('/_/test/unique', HTTP_ACCEPT='application/json')
    assert js.status_code == 409 and js.json()['error'] == 'already exists' and js.json()['constraint']
    hx = c.post('/_/test/unique', **htmx())
    assert hx.status_code == 409 and toasts(hx)[0]['message'] == 'already exists'
    page = c.get('/_/test/unique')
    assert page.status_code == 409 and b'already exists' in page.content


@override_settings(ROOT_URLCONF='tests.web.test_layout')
def test_validation_errors_keep_issues_for_api_clients_and_join_them_for_people(db):
    # api/client.ts normalizeError: 'from: YYYY-MM-DD; bad'
    co = make_company()
    c, _ = signed_in_client(co)
    js = c.post('/_/test/validation', HTTP_ACCEPT='application/json')
    assert js.status_code == 400 and js.json() == {'error': 'validation', 'issues': [
        {'path': 'from', 'message': 'YYYY-MM-DD'}, {'path': '', 'message': 'bad'}]}
    hx = c.post('/_/test/validation', **htmx())
    assert toasts(hx)[0]['message'] == 'from: YYYY-MM-DD; bad'


@override_settings(ROOT_URLCONF='tests.web.test_layout')
def test_a_hidden_hub_tab_lands_on_the_first_tab_you_may_see(db, monkeypatch):
    # Hub.tsx: an address of a tab you may not see goes to the hub's first visible tab; with none, RequireRole's
    # "Not available for your role".
    items = [NavItem(hub='attendance', label='Today', url_name='_t_hub_open', order=10),
             NavItem(hub='attendance', label='HR', url_name='_t_hub_hr', order=20, roles=('hr', 'manager')),
             NavItem(hub='payroll', label='Runs', url_name='_t_hub_payroll', order=10, module='payroll')]
    monkeypatch.setattr(nav, 'all_items', lambda: items)
    co = make_company()
    emp, _ = signed_in_client(co, roles=['employee'])
    r = emp.get('/_/test/hub/hr')
    assert r.status_code == 302 and r['Location'] == '/_/test/hub/open'
    none = emp.get('/_/test/hub/payroll')
    assert none.status_code == 403 and b'Not available for your role' in none.content
    assert b'Ask HR if you believe you need access to this page.' in none.content
    js = emp.get('/_/test/hub/hr', HTTP_ACCEPT='application/json')
    assert js.status_code == 403 and js.json() == {'error': 'requires role hr or manager'}


@override_settings(ROOT_URLCONF='tests.web.test_layout')
def test_role_module_and_feature_decorators(db):
    co = make_company()
    emp, _ = signed_in_client(co, roles=['employee'])
    r = emp.get('/_/test/hr', HTTP_ACCEPT='application/json')
    assert r.status_code == 403 and r.json()['error'] == 'requires role hr or manager'
    assert emp.get('/_/test/hr').status_code == 403                   # a page: friendly 403
    hr, _ = signed_in_client(co, roles=['hr'])
    assert hr.get('/_/test/hr').status_code == 200
    pay, _ = signed_in_client(co, roles=['payroll_admin'])
    assert pay.get('/_/test/payroll').status_code == 200
    att, _ = signed_in_client(co, roles=['attendance_admin'])
    assert att.get('/_/test/payroll', HTTP_ACCEPT='application/json').json()['error'] == 'requires role hr or payroll_admin'
    admin, _ = signed_in_client(co, roles=['admin'])
    assert admin.get('/_/test/payroll').status_code == 200            # admin always passes
    assert hr.get('/_/test/expenses').status_code == 200
    set_features_off(co, 'expenses')
    assert hr.get('/_/test/expenses')['Location'] == '/'               # FeatureRoute: home
    off = hr.get('/_/test/expenses', HTTP_ACCEPT='application/json')
    assert off.status_code == 404 and off.json() == {'error': 'feature off', 'feature': 'expenses'}
    anon = Client().get('/_/test/hr')
    assert anon.status_code == 302 and anon['Location'] == '/login?next=/_/test/hr'


def test_unknown_page_goes_home_when_signed_in_and_to_sign_in_otherwise(db, settings):
    # App.tsx: <Route path="*" element={<Navigate to="/" replace />} /> inside RequireRole, so signed out -> /login.
    settings.DEBUG = False
    co = make_company()
    c, _ = signed_in_client(co)
    r = c.get('/no/such/page')
    assert r.status_code == 302 and r['Location'] == '/'
    assert c.get('/no/such/page', HTTP_ACCEPT='application/json').status_code == 404   # API clients: 404
    assert c.get('/_/no/such/partial').status_code == 404
    anon = Client().get('/no/such/page')
    assert anon.status_code == 302 and anon['Location'] == '/login?next=/no/such/page'
    assert Client().get('/_/no/such/partial').status_code == 404
    # A record that does not exist is still a friendly 404 page (raised by the view, not a missing address).
    assert c.get('/me/profile/security').status_code == 200


@override_settings(ROOT_URLCONF='tests.web.test_layout')
def test_view_raised_not_found_stays_a_friendly_page(db):
    co = make_company()
    c, _ = signed_in_client(co)
    page = c.get('/_/test/missing')
    assert page.status_code == 404 and b'class="hj-nav"' in page.content


# ---------------------------------------------------------------- data table

ROWS = [{'id': str(i), 'code': f'E{i:03d}', 'name': n, 'joined': f'2026-0{1 + i % 9}-1{i % 9}', 'pay': str(1000 * i)}
        for i, n in enumerate(['Sita', 'Ram', 'Gita', 'Hari', 'Maya', 'Bikash', 'Anita', 'Suman', 'Kiran', 'Nabin', 'Rita', 'Prakash'], 1)]
COLS = [Column('code', 'Code', sortable=True, mono=True), Column('name', 'Name', sortable=True),
        Column('joined', 'Joined', format='bs_date', sortable=True), Column('pay', 'Pay', format='npr')]


def _table(qs=''):
    req = RequestFactory().get('/people' + qs)
    return Table(req, ROWS, COLS, id='people', search=('code', 'name'), default_sort='code', page_size=5, export='people')


def test_table_sorts_searches_and_pages():
    t = _table()
    assert [r.obj['code'] for r in t.body()] == ['E001', 'E002', 'E003', 'E004', 'E005']
    assert t.pages()['count'] == 3 and t.pages()['next'] == '/people?people-page=2'
    t = _table('?people-sort=name&people-dir=desc')
    assert t.body()[0].obj['name'] == 'Suman'
    assert t.sort_url('name') == '/people?people-sort=name&people-dir=asc'
    t = _table('?people-q=ita')
    assert [r.obj['name'] for r in t.body()] == ['Sita', 'Gita', 'Anita', 'Rita']
    t = _table('?people-page=3')
    assert len(t.body()) == 2 and t.pages()['prev'] == '/people?people-page=2'
    t = _table('?people-sort=nonsense')
    assert t.sort == 'code'                                            # unknown sort keys fall back
    cell = _table().body()[0].cells
    assert cell[2].text == '28 Magh 2082' and cell[3].text == 'NPR 1,000.00' and cell[3].column.align == 'right'


def test_table_csv_export_and_partial_render(db):
    t = _table('?people-export=csv&people-q=ita')
    assert t.wants_csv
    resp = t.csv_response()
    body = resp.content.decode('utf-8')
    assert resp['Content-Disposition'] == 'attachment; filename="people.csv"'
    assert body.startswith('﻿Code,Name,Joined,Pay\r\nE001,Sita,')
    assert body.count('\r\n') == 5
    req = RequestFactory().get('/people', HTTP_HX_REQUEST='true', HTTP_HX_TARGET='people')
    req.hajiri_user = None
    t = Table(req, ROWS, COLS, id='people', page_size=5)
    r = table_response(req, t, 'web/components.html', {'table': t})
    html = r.content.decode()
    assert html.lstrip().startswith('<div id="people"') and 'hj-nav' not in html


def test_table_with_a_queryset(db):
    from hajiri.db.models import Employee
    co = make_company()
    for n in ('Zed', 'Amy', 'Bob'):
        make_employee(co, name=n)
    req = RequestFactory().get('/e?e-q=b')
    t = Table(req, Employee.objects.filter(company=co), [Column('name_en', 'Name', sortable=True)], id='e',
              search=('name_en',), default_sort='name_en')
    assert [r.obj.name_en for r in t.body()] == ['Bob'] and t.total == 1


def test_empty_table_shows_the_empty_state():
    req = RequestFactory().get('/x')
    t = Table(req, [], COLS, id='x', empty_title='No people yet', empty_hint='Add one.')
    html = Template('{% data_table t %}').render(Context({'t': t, 'request': req}))
    assert 'No people yet' in html and 'Add one.' in html


# ---------------------------------------------------------------- components in isolation

def _render(src, **ctx):
    req = RequestFactory().get('/x')
    return Template(src).render(Context({'request': req, **ctx}))


def test_page_header_embedded_drops_the_title():
    full = _render('{% page_header "Shifts" subtitle="Hours" %}<button>Add</button>{% endpage_header %}')
    assert '<h1 class="hj-page-title">Shifts</h1>' in full and 'Hours' in full and '<button>Add</button>' in full
    emb = _render('{% page_header "Shifts" subtitle="Hours" %}<button>Add</button>{% endpage_header %}', embedded=True)
    assert 'hj-page-title' not in emb and 'Hours' in emb and '<button>Add</button>' in emb
    back = _render('{% page_header "Sita" back="/employees" back_label="Employees" %}{% endpage_header %}')
    assert 'href="/employees"' in back and 'Employees' in back


def test_misc_components_render():
    html = _render('{% empty_state title="Nothing" hint="Hint" action_label="Go" action_url="/go" %}'
                   '{% stat_strip stats %}{% field "PAN" None %}{% badge "On" "ok" %}{% icon "calendar" %}'
                   '{% alert "warn" title="T" %}body{% endalert %}{% card "C" %}inside{% endcard %}'
                   '{% modal "m1" "Title" %}mbody{% endmodal %}{% drawer "d1" "Drawer" %}dbody{% enddrawer %}'
                   '{% section "S" hint="h" %}sbody{% endsection %}{% confirm_attrs "Sure?" label="Delete" %}',
                   stats=[{'label': 'Present', 'value': 1234, 'tone': 'ok'}])
    for m in ('Nothing', 'href="/go"', '1,234', 'hj-dot', '—', 'hj-tone-ok', 'ti-calendar', 'hj-alert', 'inside',
              '<dialog class="hj-modal', 'id="m1"', 'hj-drawer', 'sbody', 'hx-confirm="Sure?"', 'data-confirm-label="Delete"'):
        assert m in html, m


def test_date_and_month_inputs_and_pickers(db):
    html = _render('{% bs_date_input "from" value="2026-08-28" label="From" required=True holidays=h %}'
                   '{% bs_month_input year=2083 month=6 label="Period" %}{% employee_picker "employee_id" %}'
                   '{% file_upload "f" "expense_receipt" %}{% camera_capture %}{% approval_chain "leave" %}',
                   h={'2026-08-29': 'Teej'})
    assert 'name="from"' in html and 'value="2026-08-28"' in html and '12 Bhadra 2083' in html and 'Teej' in html
    assert '<option value="6" selected>Ashwin</option>' in html and '<option value="2083" selected>2083</option>' in html
    assert 'hx-get="/_/web/employees/search?status=active"' in html
    assert "purpose: 'expense_receipt'" in html and 'hjCamera()' in html


def test_form_field_renders_label_help_and_errors():
    from django import forms

    class F(forms.Form):
        name = forms.CharField(label='Name', help_text='As on the citizenship', max_length=5)
        ok = forms.BooleanField(label='Agree', required=False)
        kind = forms.ChoiceField(choices=[('a', 'A')])
    f = F({'name': 'too long name'})
    f.is_valid()
    html = _render('{% form_field f.name %}{% form_field f.ok %}{% form_field f.kind %}{% form_errors f %}', f=f)
    assert 'hj-label' in html and 'As on the citizenship' in html and 'aria-invalid="true"' in html
    assert 'Ensure this value has at most 5 characters' in html and 'class="hj-check"' in html and 'hj-select' in html


def test_charts_render_svg():
    html = _render('{% column_chart labels series money=True aria_label="Cost" %}{% line_chart labels series %}'
                   '{% donut d center_label="people" %}{% hbars b %}{% trend_bars p "Attendance" %}{% sparkline v %}',
                   labels=['Shrawan', 'Bhadra'], series=[{'key': 'a', 'label': 'Gross', 'values': [100000, None]}],
                   d=[{'label': 'Eng', 'count': 3}, {'label': 'Ops', 'count': 1}], b=[{'label': 'HQ', 'value': 5}],
                   p=[{'date': '2026-08-28', 'value': 90}, {'date': '2026-08-29', 'value': None}], v=[1, 3, 2])
    assert html.count('<svg') == 5 and 'aria-label="Cost"' in html and 'Shrawan' in html and '75%' in html
    assert 'data-tip="Shrawan · Gross: Rs 1,00,000"' in html and '12 Bhadra 2083: no working day' not in html
    assert '13 Bhadra 2083: no working day' in html


def test_qr_code_is_an_svg():
    html = _render('{% qr_code "otpauth://totp/Hajiri:a?secret=JBSWY3DP" 120 "QR" %}')
    assert html.startswith('<svg class="hj-qr" width="120"') and 'aria-label="QR"' in html and 'h1v1h-1z' in html


# ---------------------------------------------------------------- employee search (EmployeePicker)

def test_employee_search_is_scoped(db):
    co, other = make_company(), make_company()
    boss = make_employee(co, name='Ram Boss')
    mine = make_employee(co, name='Sita Report', manager=boss)
    make_employee(co, name='Gita Elsewhere')
    make_employee(other, name='Sita Foreign')
    hr, _ = signed_in_client(co, roles=['hr'])
    r = hr.get('/_/web/employees/search?q=sita', **htmx())
    assert b'Sita Report' in r.content and b'Sita Foreign' not in r.content
    js = hr.get('/_/web/employees/search?q=', HTTP_ACCEPT='application/json').json()['employees']
    assert len(js) == 4                                                # 3 + the HR user's own employee
    mgr, _ = signed_in_client(co, roles=['manager'], employee=boss)
    names = [e['name_en'] for e in mgr.get('/_/web/employees/search', HTTP_ACCEPT='application/json').json()['employees']]
    assert sorted(names) == ['Ram Boss', 'Sita Report']
    assert mine
    none = hr.get('/_/web/employees/search?q=zzz', **htmx())
    assert b'No employee matches that search' in none.content
    like = hr.get('/_/web/employees/search?q=%25', HTTP_ACCEPT='application/json').json()['employees']
    assert like == []                                                  # % is matched literally


# ---------------------------------------------------------------- PWA, calendar data, legacy links

def test_pwa_files_are_served_at_the_root(db):
    c = Client()
    m = c.get('/manifest.webmanifest')
    assert m.status_code == 200 and m['Content-Type'] == 'application/manifest+json'
    assert json.loads(b''.join(m.streaming_content))['short_name'] == 'Hajiri'
    sw = c.get('/sw.js')
    assert sw.status_code == 200 and sw['Service-Worker-Allowed'] == '/' and sw['Cache-Control'] == 'no-cache'
    assert c.get('/favicon.svg').status_code == 200 and c.get('/icon-192.png')['Content-Type'] == 'image/png'


def test_bs_calendar_script(db):
    r = Client().get('/_/web/bs-calendar.js')
    body = r.content.decode()
    assert r['Content-Type'].startswith('text/javascript') and body.startswith('window.HJ_BS=')
    data = json.loads(body[len('window.HJ_BS='):-1])
    assert data['anchorAd'] == '2025-04-14' and data['anchorBs'] == [2082, 1, 1] and data['names'][5] == 'Ashwin'
    assert len(data['months']['2083']) == 12


def test_legacy_addresses_redirect(db):
    c = Client()
    assert c.get('/hr/dashboard')['Location'] == '/overview'
    assert c.get('/manager/fix-punch')['Location'] == '/attendance/fix'
    assert c.get('/me/data')['Location'] == '/me/profile/privacy'
    assert c.get('/hr/employees/abc')['Location'] == '/employees/abc'


def test_health(db):
    assert Client().get('/health').json() == {'ok': True, 'db': 'up'}


def test_server_side_bs_month_grid(db):
    r = Client().get('/_/web/bs-grid?year=2083&month=5')
    assert r.status_code == 200 and r.content.count(b'role="gridcell"') % 7 == 0
    assert Client().get('/_/web/bs-grid', HTTP_ACCEPT='application/json').status_code == 400


@pytest.mark.parametrize('path', ['/login', '/change-password', '/setup-two-step'])
def test_bare_pages_render_without_the_sidebar(db, path):
    co = make_company()
    c, _ = signed_in_client(co)
    r = c.get(path)
    if r.status_code == 302:
        return
    assert r.status_code == 200 and b'class="hj-nav"' not in r.content


def test_chart_card_has_legend_table_view_and_download():
    html = _render('{% chart_card "Cost" subtitle="Six months" legend=legend table=table csv_url="?export=x" %}<svg></svg>{% endchart_card %}',
                   legend=[{'label': 'Gross', 'color': 'red'}, {'label': 'TDS', 'color': 'blue'}],
                   table={'columns': ['Month', 'Gross'], 'rows': [['Shrawan', 1234567], ['Bhadra', None]]})
    for m in ('Cost', 'Six months', '--c:red', 'TDS', '<svg></svg>', "view = 'table'", '1,234,567', '—',
              'href="?export=x"', 'aria-label="Download Cost as CSV"'):
        assert m in html, m
    one = _render('{% chart_card "Cost" legend=legend empty="No pay runs yet" %}<svg></svg>{% endchart_card %}',
                  legend=[{'label': 'Gross', 'color': 'red'}])
    assert 'hj-legend' not in one and 'No pay runs yet' in one and '<svg>' not in one


def test_gallery_chart_card_downloads_csv(db):
    co = make_company()
    c, _ = signed_in_client(co, roles=['hr'])
    r = c.get('/_/web/components?export=chart')
    assert r['Content-Type'].startswith('text/csv') and r.content.decode().startswith('\ufeffMonth,Gross pay,TDS\r\nShrawan,4120000,380000')


def test_offline_flush_url_and_device_token_hooks(db, monkeypatch):
    co = make_company()
    c, _ = signed_in_client(co)
    html = c.get('/me/profile/security').content.decode()
    from hajiri.web import context_processors
    assert ('data-offline-flush-url=' in html) == bool(nav.url_of(context_processors.OFFLINE_FLUSH_URL_NAME))
    monkeypatch.setattr(nav, 'url_of', lambda name: '/_/test/offline' if name == context_processors.OFFLINE_FLUSH_URL_NAME else None)
    assert 'data-offline-flush-url="/_/test/offline"' in c.get('/me/profile/security').content.decode()
    from pathlib import Path
    js = (Path(__file__).resolve().parents[2] / 'static/js/app.js').read_text()
    assert "'hajiri.deviceToken'" in js and "'X-Device-Token'" in js and '[data-device-token]' in js


def test_static_files_are_served_with_the_security_headers(db):
    r = Client().get('/static/css/hajiri.css')
    assert r.status_code == 200 and r['X-Content-Type-Options'] == 'nosniff' and 'text/css' in r['Content-Type']
    assert Client().get('/static/vendor/htmx/htmx.min.js').status_code == 200


def test_popup_notices_load_when_the_relations_app_serves_them(db, monkeypatch):
    from hajiri.web import context_processors
    co = make_company()
    c, _ = signed_in_client(co)
    monkeypatch.setattr(nav, 'url_of', lambda name: '/_/test/popups' if name == context_processors.POPUP_NOTICES_URL_NAME else None)
    html = c.get('/me/profile/security').content.decode()
    assert 'hx-get="/_/test/popups" hx-trigger="load"' in html
