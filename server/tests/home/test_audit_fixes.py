"""Gaps found by the completeness audit against SettingsPage.tsx and HomePage.tsx."""
import pytest
from django.test import Client
from django.http import QueryDict

from tests.support.sql import execute, one
from hajiri.home import services as s
from tests.web.factories import set_features_off


def client_for(login) -> Client:
    c = Client()
    c.cookies['hajiri_session'] = login.token
    return c


@pytest.fixture
def hr(login_as):
    return client_for(login_as(['hr']))


def stored(demo, key):
    r = one('SELECT value FROM company_setting WHERE company_id = %s AND key = %s', [demo['company_id'], key])
    return r['value'] if r else None


def q(**kw) -> QueryDict:
    d = QueryDict(mutable=True)
    for k, v in kw.items():
        d[k] = v
    return d


# ---------------------------------------------------------------- GenericSettings / AttendanceRules state `p`

def test_generic_body_leaves_out_values_the_setting_does_not_have_until_changed():
    fields = s.GENERIC['payroll'][3]
    stored_value = {'prorationBasis': 'bs_calendar_days', 'hourlyDivisor': 208, 'payDay': 25, 'rounding': 'half_up_rupee',
                    'separateApprover': False, 'encashmentDivisor': 'bs_days'}
    # publishOnLock is not stored: its switch shows off and an untouched switch is not sent.
    post = q(prorationBasis='bs_calendar_days', hourlyDivisor='208', payDay='25', rounding='half_up_rupee', encashmentDivisor='bs_days')
    assert 'publishOnLock' not in s.generic_body(fields, stored_value, post)
    assert s.generic_body(fields, stored_value, q(**{**post.dict(), 'publishOnLock': 'on'}))['publishOnLock'] is True
    # A stored switch turned off is sent as false.
    assert s.generic_body(fields, {**stored_value, 'publishOnLock': True}, post)['publishOnLock'] is False
    # A number the setting lacks, left empty, is not sent as 0 (the server's default applies); once typed, it is.
    leave = s.GENERIC['leave'][3]
    assert 'maxContinuousDays' not in s.generic_body(leave, {'leaveYear': 'fiscal'}, q(leaveYear='fiscal', maxContinuousDays=''))
    assert s.generic_body(leave, {'leaveYear': 'fiscal'}, q(leaveYear='fiscal', maxContinuousDays='12'))['maxContinuousDays'] == 12
    # A stored number emptied is Number('') = 0, as in TS.
    assert s.generic_body(leave, {'maxContinuousDays': 30}, q(maxContinuousDays=''))['maxContinuousDays'] == 0
    # An unset select posts its blank placeholder, which is left out.
    assert 'leaveYear' not in s.generic_body(leave, {}, q(leaveYear=''))


def test_attendance_body_keeps_unset_values_unset():
    post = q(otMinThresholdMin='30', otRoundingMin='15', nightPremiumRate='', autoClockOutAfterMin='0',
             regularisationBackdateDays='30', regularisationMonthlyCap='5', wfhPhotoRetentionDays='', wfhPhotoRequired='on',
             holidayWork='substitute_leave')
    body = s.attendance_body({'otMinThresholdMin': 30, 'otRoundingMin': 15}, post)
    # Empty boxes for values the policy lacks, switches still showing their unset look, and the select's shown
    # default are not sent; the server fills its defaults.
    for k in ('nightPremiumRate', 'wfhPhotoRetentionDays', 'wfhPhotoRequired', 'requirePreapproval', 'nightWindowEnabled', 'holidayWork'):
        assert k not in body, k
    assert body['otMinThresholdMin'] == 30 and body['autoClockOutAfterMin'] == 0
    # Changing them sends them.
    body = s.attendance_body({}, q(**{**post.dict(), 'holidayWork': 'pay', 'requirePreapproval': 'on', 'wfhPhotoRequired': ''}))
    assert body['holidayWork'] == 'pay' and body['requirePreapproval'] is True and body['wfhPhotoRequired'] is False


def r_ok(html: str) -> bool:
    return 'Time tracking' in html


# ---------------------------------------------------------------- Home card
