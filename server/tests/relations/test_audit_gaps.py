"""Gaps found by the completeness audit against routes.ts and the React pages."""
from tests.support.sql import one
from hajiri.relations import services as S


from .conftest import client_for


def js(v: str) -> str:
    """How |escapejs writes a value into an Alpine string."""
    from django.utils.html import escapejs
    return escapejs(v)


def test_an_empty_hearing_place_is_stored_as_sent(world):
    w = world
    S.set_committee(w.hr, {'userIds': [w.member.id]})
    gid = S.file_grievance(w.sita, {'category': 'other', 'subject': 'Parking', 'description': 'No parking space'})['id']
    S.update_grievance(w.member, gid, {'hearingAt': '2026-11-01T10:00:00+05:45', 'hearingPlace': ''})
    assert one('SELECT hearing_place FROM grievance WHERE id = %s', [gid])['hearing_place'] == ''
    n = one("SELECT body FROM notification_outbox WHERE template = 'relations.grievance.hearing' AND channel = 'in_app'")
    assert n['body'] == 'The committee will hear your grievance on 15 Kartik 2083.'
