"""The page forms of the home slice (forms.py): how the HTML posts become the services' bodies."""
import pytest
from django.http import QueryDict

from hajiri.core.errors import ValidationError
from hajiri.home import forms, services as s


def q(**kw) -> QueryDict:
    d = QueryDict(mutable=True)
    for k, v in kw.items():
        if isinstance(v, list):
            d.setlist(k, v)
        else:
            d[k] = v
    return d


def test_number_boxes_whole_numbers_stay_ints_and_text_is_refused():
    leave = s.GENERIC['leave'][3]
    assert s.generic_body(leave, {}, q(maxContinuousDays='12.0'))['maxContinuousDays'] == 12
    assert s.generic_body(leave, {}, q(maxContinuousDays=' 2.5 '))['maxContinuousDays'] == 2.5
    with pytest.raises(ValidationError) as e:
        s.generic_body(leave, {}, q(maxContinuousDays='twelve'))
    assert e.value.issues == [{'path': 'maxContinuousDays', 'message': 'Expected number, received NaN'}]


def test_switches_count_only_on_true_or_1():
    notify = s.GENERIC['payroll'][3]
    for v, want in (('on', True), ('true', True), ('1', True), ('yes', False), ('', False)):
        assert s.generic_body(notify, {'separateApprover': not want}, q(separateApprover=v))['separateApprover'] is want, v


def test_late_penalty_keeps_stored_numbers_while_switched_off():
    stored = {'latePenalty': {'enabled': True, 'lateCount': 4, 'deductionDays': 1}}
    assert s.attendance_body(stored, q())['latePenalty'] == {'enabled': False, 'lateCount': 4, 'deductionDays': 1}
    on = q(**{'latePenalty.enabled': 'on', 'latePenalty.lateCount': '5', 'latePenalty.deductionDays': '0.5'})
    assert s.attendance_body(stored, on)['latePenalty'] == {'enabled': True, 'lateCount': 5, 'deductionDays': 0.5}


def test_punch_form_drops_a_location_it_cannot_read():
    b = forms.PunchForm(q(direction='out', lat='27.7', lng='x', accuracy='12', reason='  late bus ')).body('T')
    assert b['direction'] == 'out' and b['clientTime'] == 'T' and 'lat' not in b and b['reason'] == 'late bus'
    b = forms.PunchForm(q(lat='27.7', lng='85.3', accuracy='bad')).body('T')
    assert b['lat'] == 27.7 and b['lng'] == 85.3 and 'accuracyM' not in b and b['direction'] == 'in'
    assert len(b['idempotencyKey']) == 36


def test_user_forms_build_the_core_users_bodies():
    b = forms.UserNewForm(q(email=' a@b.test ', name='A', password=' p ', roles=['hr', 'employee'], employee_id='')).body()
    assert b == {'email': 'a@b.test', 'name': 'A', 'password': ' p ', 'roles': ['hr', 'employee']}
    assert forms.UserUpdateForm(q(roles_sent='1')).body() == {'roles': []}
    assert forms.UserUpdateForm(q(status='disabled')).body() == {'status': 'disabled'}
    with pytest.raises(ValidationError):
        forms.ResetTwoFactorForm(q(reason=' ab ')).reason_text()
    with pytest.raises(ValidationError):
        forms.ResetTwoFactorForm(q()).reason_text()
    assert forms.ResetTwoFactorForm(q(reason=' Lost phone ')).reason_text() == 'Lost phone'
