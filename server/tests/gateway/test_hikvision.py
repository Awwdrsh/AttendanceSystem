"""Port of apps/gateway/src/hikvision.test.ts."""
import json

import pytest

from hajiri.gateway.hikvision import parse_hik_event, parse_iso_with_offset, parse_multipart
from hajiri.gateway.store import MemoryStore


@pytest.fixture
def hik(make_app):
    store = MemoryStore()
    device_id = store.add_device('HIK-01', vendor='hikvision', protocol='isapi').id
    store.mappings[f'{device_id}:1'] = 'emp-1'
    return store, make_app(store)


def event(over=None, ace=None):
    ev = {'ipAddress': '10.0.0.5', 'dateTime': '2026-09-21T10:17:03+05:45', 'eventType': 'AccessControllerEvent',
          'eventState': 'active',
          'AccessControllerEvent': {'deviceName': 'Front door', 'majorEventType': 5, 'subEventType': 75, 'employeeNoString': '1',
                                    'serialNo': 'HIK-01', 'currentVerifyMode': 'face', 'attendanceStatus': 'checkIn',
                                    **(ace or {})}}
    ev.update(over or {})
    return ev


class TestHikvisionListeningHost:
    def test_ingests_a_json_face_event_with_the_0545_offset_and_queues_recompute(self, hik):
        store, app = hik
        r = app.post('/hikvision/event?sn=HIK-01', event())
        assert r.status == 200
        assert len(store.punches) == 1
        p = store.punches[0]
        assert (p.vendor, p.device_user_pin, p.employee_id, p.verify_mode, p.direction, p.local_date, p.device_local_time) == (
            'hikvision', '1', 'emp-1', 'face', 'in', '2026-09-21', '2026-09-21 10:17:03')
        assert p.punched_at_iso == '2026-09-21T04:32:03.000Z'
        # A morning punch also re-evaluates the previous day (a night shift's punch-out belongs to the day it started).
        assert store.recompute_queue == [
            {'employeeId': 'emp-1', 'localDate': '2026-09-20', 'reason': 'punch:hikvision'},
            {'employeeId': 'emp-1', 'localDate': '2026-09-21', 'reason': 'punch:hikvision'}]

    def test_dedupes_resent_events_and_does_not_dedupe_across_devices(self, hik):
        store, app = hik
        app.post('/hikvision/event?sn=HIK-01', event())
        app.post('/hikvision/event?sn=HIK-01', event())
        app.post('/hikvision/event?sn=HIK-01', event({'dateTime': '2026-09-21T10:17:06+05:45'}))
        assert len(store.punches) == 1
        store.add_device('HIK-02', vendor='hikvision', protocol='isapi')
        app.post('/hikvision/event?sn=HIK-02', event())
        assert len(store.punches) == 2

    def test_parses_multipart_with_event_log_and_picture_parts(self, hik):
        store, app = hik
        b = 'xx-boundary-7d'
        body = b''.join([
            f'--{b}\r\nContent-Disposition: form-data; name="event_log"\r\nContent-Type: application/json\r\n\r\n'
            f'{json.dumps(event({}, {"subEventType": 113}))}\r\n'.encode(),
            f'--{b}\r\nContent-Disposition: form-data; name="Picture"; filename="1.jpg"\r\nContent-Type: image/jpeg\r\n\r\n'.encode(),
            bytes([0xff, 0xd8, 0xff, 0x00, 0x0d, 0x0a, 0x01]),
            f'\r\n--{b}--\r\n'.encode(),
        ])
        r = app.post('/hikvision/event?sn=HIK-01', body, content_type=f'multipart/form-data; boundary={b}')
        assert r.status == 200
        assert len(store.punches) == 1
        assert store.punches[0].verify_mode == 'fingerprint'

    def test_heartbeat_touches_last_seen_non_attendance_ignored_xml_acknowledged(self, hik):
        store, app = hik
        app.post('/hikvision/event?sn=HIK-01', {'ipAddress': '10.0.0.5', 'dateTime': '2026-09-21T10:00:00+05:45', 'eventType': 'heartBeat'})
        assert store.devices['HIK-01'].last_seen_at is not None
        app.post('/hikvision/event?sn=HIK-01', event({}, {'subEventType': 76}))   # face auth failed
        r = app.post('/hikvision/event?sn=HIK-01', '<EventNotificationAlert><eventType>x</eventType></EventNotificationAlert>',
                     content_type='application/xml')
        assert r.status == 200
        assert store.punches == []

    def test_unknown_serial_becomes_pending_and_is_rejected_serial_from_body(self, hik):
        store, app = hik
        r = app.post('/hikvision/event', event({}, {'serialNo': 'HIK-NEW'}))
        assert r.status == 403
        d = store.devices['HIK-NEW']
        assert (d.vendor, d.protocol, d.status) == ('hikvision', 'isapi', 'pending')
        assert app.post('/hikvision/event', {'eventType': 'heartBeat'}).status == 400

    def test_parser_helpers(self):
        assert parse_iso_with_offset('2026-09-21T10:17:03+05:45') == {'local': '2026-09-21 10:17:03', 'offsetMin': 345}
        assert parse_iso_with_offset('2026-09-21T04:32:03Z') == {'local': '2026-09-21 04:32:03', 'offsetMin': 0}
        assert parse_iso_with_offset('2026-09-21 10:17:03') == {'local': '2026-09-21 10:17:03', 'offsetMin': None}
        ev = parse_hik_event(event({}, {'subEventType': 38, 'attendanceStatus': 'checkOut'}))
        assert (ev.kind, ev.verify_mode, ev.direction) == ('attendance', 'card', 'out')
        assert parse_hik_event({'eventType': 'heartBeat'}).kind == 'heartbeat'
        parts = parse_multipart(b'--b\r\nContent-Disposition: form-data; name="a"\r\n\r\nhello\r\n--b--\r\n',
                                'multipart/form-data; boundary=b')
        assert parts['a']['data'] == b'hello'


def test_integral_float_event_codes_behave_like_js_numbers():
    from hajiri.gateway.hikvision import parse_hik_event
    ev = parse_hik_event({'dateTime': '2026-09-21T10:17:03+05:45', 'eventType': 'AccessControllerEvent',
                          'AccessControllerEvent': {'majorEventType': 5.0, 'subEventType': 75.0, 'employeeNoString': '1'}})
    assert (ev.kind, ev.verify_mode, ev.major, ev.minor) == ('attendance', 'face', 5, 75)
    assert type(ev.raw['major']) is int and type(ev.raw['minor']) is int
