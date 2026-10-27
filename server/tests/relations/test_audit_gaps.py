"""Gaps found by the completeness audit against routes.ts and the React pages."""
from hajiri.core.db import one
from hajiri.relations import services as S
from tests.web.factories import htmx

from .conftest import client_for


def js(v: str) -> str:
    """How |escapejs writes a value into an Alpine string."""
    from django.utils.html import escapejs
    return escapejs(v)


def test_a_malformed_id_is_a_400_as_uuid_parse_answers_in_ts(world):
    w = world
    hr = client_for(w.hr)
    for url in ('/_/relations/grievances/not-a-uuid', '/_/relations/cases/not-a-uuid', '/_/relations/kye/not-a-uuid',
                '/_/relations/employee/not-a-uuid'):
        assert hr.get(url, **htmx('hj-drawer-body')).status_code == 400, url
    assert hr.post('/_/relations/devices/not-a-uuid/approve', {}, **htmx()).status_code == 400
    assert client_for(w.sita).post('/_/relations/grievances/not-a-uuid/withdraw', {}, **htmx()).status_code == 400


def test_an_empty_hearing_place_is_stored_as_sent(world):
    w = world
    S.set_committee(w.hr, {'userIds': [w.member.id]})
    gid = S.file_grievance(w.sita, {'category': 'other', 'subject': 'Parking', 'description': 'No parking space'})['id']
    S.update_grievance(w.member, gid, {'hearingAt': '2026-11-01T10:00:00+05:45', 'hearingPlace': ''})
    assert one('SELECT hearing_place FROM grievance WHERE id = %s', [gid])['hearing_place'] == ''
    n = one("SELECT body FROM notification_outbox WHERE template = 'relations.grievance.hearing' AND channel = 'in_app'")
    assert n['body'] == 'The committee will hear your grievance on 15 Kartik 2083.'


def test_buttons_stay_off_until_the_form_is_complete(world):
    w = world
    hr = client_for(w.hr)
    S.set_committee(w.hr, {'userIds': [w.member.id]})
    gid = S.file_grievance(w.sita, {'category': 'other', 'subject': 'Parking', 'description': 'No parking space'})['id']
    # GrievanceThread: the hearing Save needs a date and a time.
    thread = hr.get(f'/_/relations/grievances/{gid}', **htmx('hj-drawer-body')).content.decode()
    assert ':disabled="!date || !time"' in thread and 'date: false' in thread
    # AssignmentForm: the employee and both dates are needed.
    form = hr.get('/_/relations/assignments/new', **htmx('hj-modal-body')).content.decode()
    assert '!emp || !start || !end' in form
    assert "emp: '" + js(str(w.sita_e.id)) + "'" in hr.get(f'/_/relations/assignments/new?employee={w.sita_e.id}', **htmx('hj-modal-body')).content.decode()
    # OpenCase: the employee and the incident date are needed.
    assert '!emp || !date || !category' in hr.get('/_/relations/cases/new', **htmx('hj-modal-body')).content.decode()
    # EmployeeRelationsSection: Save is off while the contract end is unchanged.
    S.set_contract(w.hr, str(w.sita_e.id), {'contractEnd': '2027-01-01'})
    tab = hr.get(f'/_/relations/employee/{w.sita_e.id}', **htmx()).content.decode()
    assert ":disabled=\"cur === orig\"" in tab and "orig: '" + js('2027-01-01') + "'" in tab
