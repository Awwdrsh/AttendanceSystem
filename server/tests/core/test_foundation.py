"""The idiomatic foundation: DRF integration, serializer validation in the TS shape, ORM expressions for the SQL
functions, company scoping, collation, images, email and settings storage."""
import io
import json
from datetime import date

import pytest
from django.core import mail
from django.db.models import F, Value
from django.test import Client, override_settings
from django.urls import path
from rest_framework import serializers
from rest_framework.decorators import api_view, permission_classes
from rest_framework.response import Response

from hajiri.core.api import FeatureEnabled, HasModule, HasRole, IsSignedIn
from hajiri.core.collation import collate_key, locale_compare, sort_by_name
from hajiri.core.errors import ConflictError, NotFoundError, ValidationError
from hajiri.core.images import square_jpeg
from hajiri.core.mail import EmailSender, email_settings
from hajiri.core.scoping import as_uuid, get_or_404
from hajiri.core.settings_store import get_setting, put_setting, setting_subquery
from hajiri.core.validation import HhmmField, IsoDateField, validate
from hajiri.db.models import AppUser, AttendanceDay, Company, Employee, HelpdeskTicket, Manages
from hajiri.db.models.base import IsHead
from tests.web.factories import bearer, make_company, make_employee, make_user, set_features_off, sign_in


# ---------------------------------------------------------------- DRF views wired with the project defaults

class EchoIn(serializers.Serializer):
    name = serializers.CharField(min_length=1, max_length=5)
    when = IsoDateField()
    at = HhmmField(required=False)
    kind = serializers.ChoiceField(('a', 'b'), default='a')


@api_view(['GET', 'POST'])
def _echo(request):
    if request.method == 'POST':
        return Response(validate(EchoIn, request.data))
    return Response({'id': request.user.id, 'company': request.user.company_id})


@api_view(['GET'])
@permission_classes([HasRole('hr')])
def _hr_only(request):
    return Response({'ok': True})


@api_view(['GET'])
@permission_classes([HasModule('payroll')])
def _payroll(request):
    return Response({'ok': True})


@api_view(['GET'])
@permission_classes([IsSignedIn, FeatureEnabled('expenses')])
def _feature(request):
    return Response({'ok': True})


@api_view(['POST'])
def _conflict(request):
    raise ConflictError('already there')


urlpatterns = [
    path('api/t/echo', _echo), path('api/t/hr', _hr_only), path('api/t/payroll', _payroll),
    path('api/t/feature', _feature), path('api/t/conflict', _conflict),
]


@pytest.fixture
def co(db):
    return make_company()


@override_settings(ROOT_URLCONF='tests.core.test_foundation')
def test_drf_authentication_permissions_and_errors(co):
    user = make_user(co, roles=['employee'])
    c = Client()
    token = sign_in(c, user)
    assert c.get('/api/t/echo').json() == {'id': user.id, 'company': str(co.id)}
    assert Client().get('/api/t/echo').json() == {'error': 'unauthorized'}
    assert Client().get('/api/t/echo').status_code == 401
    assert Client().get('/api/t/echo', **bearer(token)).status_code == 200
    r = c.get('/api/t/hr')
    assert r.status_code == 403 and r.json() == {'error': 'requires role hr'}
    assert c.get('/api/t/payroll').json() == {'error': 'requires role hr or payroll_admin'}
    set_features_off(co, 'expenses')
    assert c.get('/api/t/feature').json() == {'error': 'feature off', 'feature': 'expenses'}
    # Wrong method answers like Express (404), and typed errors keep the TS body.
    assert c.delete('/api/t/echo').json() == {'error': 'not found'}
    assert Client().post('/api/t/conflict', **bearer(token)).json() == {'error': 'already there'}


@override_settings(ROOT_URLCONF='tests.core.test_foundation')
def test_cookie_sessions_refuse_a_foreign_origin_bearer_does_not(co):
    """As the TS sameOriginGuard: a cookie write is refused only when its Origin is another site; the React app's
    own writes carry no CSRF token."""
    user = make_user(co)
    c = Client(enforce_csrf_checks=True)
    token = sign_in(c, user)
    r = c.post('/api/t/echo', json.dumps({'name': 'a', 'when': '2026-10-01'}), content_type='application/json',
               HTTP_ORIGIN='https://evil.example')
    assert r.status_code == 403 and r.json() == {'error': 'cross-site request refused'}
    r = c.post('/api/t/echo', json.dumps({'name': 'a', 'when': '2026-10-01'}), content_type='application/json',
               HTTP_ORIGIN='http://testserver')
    assert r.status_code == 200
    b = Client(enforce_csrf_checks=True)
    r = b.post('/api/t/echo', json.dumps({'name': 'a', 'when': '2026-10-01'}), content_type='application/json', **bearer(token))
    assert r.status_code == 200 and r.json() == {'name': 'a', 'when': '2026-10-01', 'kind': 'a'}


@override_settings(ROOT_URLCONF='tests.core.test_foundation')
def test_validation_issues_use_the_zod_wording(co):
    token = sign_in(Client(), make_user(co))
    r = Client().post('/api/t/echo', json.dumps({'name': '', 'when': '2026-13-40', 'at': '25:00', 'kind': 'z'}),
                      content_type='application/json', **bearer(token))
    assert r.status_code == 400
    assert r.json() == {'error': 'validation', 'issues': [
        {'path': 'name', 'message': 'Too small: expected string to have >=1 characters'},
        {'path': 'when', 'message': 'not a valid date'},
        {'path': 'at', 'message': 'HH:MM'},
        {'path': 'kind', 'message': 'Invalid option: expected one of "a"|"b"'},
    ]}
    r = Client().post('/api/t/echo', 'not json', content_type='application/json', **bearer(token))
    assert r.status_code == 400 and r.json() == {'error': 'invalid JSON body'}


def test_validate_outside_views():
    with pytest.raises(ValidationError) as e:
        validate(EchoIn, {'name': 'toolong', 'when': '01-02-2026'})
    assert e.value.issues == [{'path': 'name', 'message': 'Too big: expected string to have <=5 characters'},
                              {'path': 'when', 'message': 'YYYY-MM-DD'}]
    with pytest.raises(ValidationError) as e:
        validate(EchoIn, {})
    assert {i['message'] for i in e.value.issues} == {'Required'}


@override_settings(ROOT_URLCONF='config.urls')
def test_api_docs_are_for_admins(co):
    admin, staff = Client(), Client()
    sign_in(admin, make_user(co, roles=['admin']))
    sign_in(staff, make_user(co, roles=['hr']))
    r = admin.get('/api/schema', HTTP_ACCEPT='application/json')
    assert r.status_code == 200 and '/api/v1/helpdesk/tickets' in r.json()['paths']
    assert staff.get('/api/schema').status_code == 403
    assert admin.get('/api/docs').status_code == 200


# ---------------------------------------------------------------- ORM expressions and scoping

def test_manages_and_is_head_as_orm_expressions(co):
    boss = make_employee(co, name='Boss')
    report = make_employee(co, manager=boss)
    other = make_employee(co)
    mine = Employee.objects.for_company(co.id).filter(Manages(boss.id, F('id'), date.today().isoformat()))
    assert list(mine.values_list('id', flat=True)) == [report.id]
    flags = dict(Employee.objects.for_company(co.id).annotate(head=IsHead(F('id'))).values_list('id', 'head'))
    assert flags[other.id] is False
    from hajiri.core.auth import is_manager_of
    assert is_manager_of(str(boss.id), str(report.id)) and not is_manager_of(str(boss.id), str(other.id))


def test_company_scoping_hides_other_companies(co):
    other = make_company()
    e = make_employee(other)
    assert not Employee.objects.for_company(co.id).filter(pk=e.id).exists()
    with pytest.raises(NotFoundError, match='employee not found'):
        get_or_404(Employee, co.id, 'employee not found', pk=e.id)
    assert get_or_404(Employee, other.id, pk=str(e.id)) == e
    assert AttendanceDay.objects.for_company(co.id).query.where  # scoped through employee
    assert HelpdeskTicket.company_scope == 'company' and AttendanceDay.company_scope == 'employee__company'
    assert Employee.objects.for_company(None).count() == 0
    with pytest.raises(Exception):
        as_uuid('nope')


def test_choices_and_str():
    assert HelpdeskTicket.Status.IN_PROGRESS == 'in_progress' and HelpdeskTicket.Status.IN_PROGRESS.label == 'In progress'
    assert AppUser.Role.PAYROLL_ADMIN.label == 'Payroll admin'
    assert str(Company(code='X', name_en='Xyz')) == 'Xyz'


def test_settings_store(co):
    assert get_setting(co.id, 'x.y', {'d': 1}) == {'d': 1}
    put_setting(co.id, 'x.y', {'n': 5})
    put_setting(co.id, 'x.y', {'n': 6})
    assert get_setting(co.id, 'x.y') == {'n': 6}
    from django.db.models import IntegerField, OuterRef
    n = Company.objects.filter(pk=co.id).annotate(
        n=setting_subquery('x.y', 'n', company_ref=OuterRef('pk'), output_field=IntegerField())).get().n
    assert n == 6


# ---------------------------------------------------------------- standard-library helpers

def test_collation_orders_like_locale_compare():
    names = ['Zoë', 'ram', 'Ángela', 'bob', 'Ram', 'Angela', 'éva']
    assert sort_by_name(names) == ['Angela', 'Ángela', 'bob', 'éva', 'ram', 'Ram', 'Zoë']
    assert locale_compare('a', 'B') == -1 and locale_compare('b', 'b') == 0
    assert collate_key(None) == collate_key('')


def test_square_jpeg():
    from PIL import Image
    from hajiri.core.errors import BadRequestError
    buf = io.BytesIO()
    Image.new('RGB', (1200, 800), (200, 10, 10)).save(buf, 'PNG')
    out = Image.open(io.BytesIO(square_jpeg(buf.getvalue())))
    assert out.format == 'JPEG' and out.size == (320, 320)
    with pytest.raises(BadRequestError):
        square_jpeg(b'not an image')


@override_settings(EMAIL_BACKEND='django.core.mail.backends.locmem.EmailBackend')
def test_email_sender_and_smtp_url():
    class Row:
        id, title, body, recipient_email = 'n1', 'Hello', 'Body text', 'sita@example.com'
    EmailSender().send(Row())
    assert mail.outbox[0].subject == 'Hello' and mail.outbox[0].to == ['sita@example.com']
    s = email_settings('smtp://u%40x:p@mail.example.com:2525?from=hr@example.com')
    assert (s['EMAIL_HOST'], s['EMAIL_PORT'], s['EMAIL_HOST_USER'], s['DEFAULT_FROM_EMAIL']) == \
        ('mail.example.com', 2525, 'u@x', 'hr@example.com')
    assert email_settings('smtps://a:b@h')['EMAIL_USE_SSL'] is True
    assert email_settings('')['EMAIL_BACKEND'].endswith('console.EmailBackend')
    with pytest.raises(ValueError):
        email_settings('http://nope')
    with pytest.raises(Exception):
        Row.recipient_email = None
        EmailSender().send(Row())
