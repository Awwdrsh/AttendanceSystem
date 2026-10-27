"""Hikvision ISAPI "HTTP listening host" events (plan Appendix C). Port of apps/gateway/src/hikvision/parse.ts.

The terminal POSTs JSON (or multipart with an event_log JSON part plus an optional Picture part). Field names come
from community captures, not official docs (NDA): verify on the purchased unit. Attendance filter: majorEventType 5
with subEventType 75 (face), 113 (fingerprint), 38 (card).
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .adms import is_real_datetime
from .jsutil import js_string_of_number, js_trim, js_truthy

ATTENDANCE_MINOR = {75: 'face', 113: 'fingerprint', 38: 'card', 1: 'card'}
DIRECTION = {'checkin': 'in', 'checkout': 'out', 'breakout': 'out', 'breakin': 'in', 'overtimein': 'in',
             'overtimeout': 'out', 'overtimein_': 'in'}

_UNDEF = object()   # a key the payload did not carry (JSON.stringify drops it; null is kept)


@dataclass
class HikEvent:
    kind: str                       # attendance | heartbeat | other
    serial_no: str | None
    device_local_time: str | None   # 'YYYY-MM-DD HH:MM:SS'
    tz_offset_min: int | None       # from the dateTime offset, e.g. +05:45
    pin: str | None
    verify_mode: str
    direction: str                  # in | out | unknown
    major: float | None
    minor: float | None
    raw: dict = field(default_factory=dict)


_ISO_RE = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2})[T ]([0-9]{2}):([0-9]{2}):([0-9]{2})(?:\.[0-9]+)?(Z|[+-][0-9]{2}:?[0-9]{2})?')


def parse_iso_with_offset(s: str) -> dict | None:
    """Parse '2026-09-21T10:17:03+05:45' into local time text and the offset in minutes."""
    m = _ISO_RE.fullmatch(js_trim(s))
    if not m:
        return None
    g = m.groups()
    if not is_real_datetime(*(int(x) for x in g[:6])):
        return None
    z = g[6]
    offset: int | None = None
    if z == 'Z':
        offset = 0
    elif z:
        sign = -1 if z.startswith('-') else 1
        offset = sign * (int(z[1:3]) * 60 + int(z[-2:]))
    return {'local': f'{g[0]}-{g[1]}-{g[2]} {g[3]}:{g[4]}:{g[5]}', 'offsetMin': offset}


def _str(v) -> str | None:
    if isinstance(v, str):
        t = js_trim(v)
        return t or None
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        return js_string_of_number(v)
    return None


def _num(v):
    if isinstance(v, (int, float)) and not isinstance(v, bool):
        # JSON.parse('5.0') is the number 5: keep integral values as int so raw.major stores as 5, not 5.0.
        return int(v) if isinstance(v, float) and v.is_integer() and abs(v) <= 2 ** 53 else v
    if isinstance(v, str) and re.fullmatch(r'[0-9]+', v):
        return int(v)
    return None


def _obj(v) -> dict:
    """`v && typeof v === 'object'`: arrays pass the test in JS but carry no named fields."""
    return v if isinstance(v, dict) else {}


def parse_hik_event(payload) -> HikEvent:
    root = _obj(payload)
    ace = _obj(root.get('AccessControllerEvent'))
    event_type = (_str(root.get('eventType')) or '').lower()
    major = _num(ace.get('majorEventType'))
    minor = _num(ace.get('subEventType'))
    serial = _str(root.get('serialNo')) or _str(ace.get('serialNo')) or _str(ace.get('deviceID')) or _str(root.get('deviceID'))
    dt = _str(root.get('dateTime')) or _str(ace.get('dateTime'))
    parsed = parse_iso_with_offset(dt) if dt else None
    raw_src = {
        'eventType': root.get('eventType', _UNDEF), 'major': major, 'minor': minor,
        'employeeNoString': ace.get('employeeNoString', _UNDEF), 'cardNo': ace.get('cardNo', _UNDEF),
        'currentVerifyMode': ace.get('currentVerifyMode', _UNDEF), 'attendanceStatus': ace.get('attendanceStatus', _UNDEF),
        'deviceName': ace.get('deviceName', _UNDEF), 'ipAddress': root.get('ipAddress', _UNDEF),
    }
    raw = {k: v for k, v in raw_src.items() if v is not _UNDEF}
    base = dict(serial_no=serial, device_local_time=parsed['local'] if parsed else None,
                tz_offset_min=parsed['offsetMin'] if parsed else None, major=major, minor=minor, raw=raw)
    if (event_type == 'heartbeat' or (event_type == 'accesscontrollerevent' and major == 5 and minor == 0)
            or (major is None and minor is None and not js_truthy(ace.get('employeeNoString')))):
        return HikEvent(kind='heartbeat', pin=None, verify_mode='none', direction='unknown', **base)
    pin = _str(ace.get('employeeNoString')) or _str(ace.get('employeeNo'))
    verify = ATTENDANCE_MINOR.get(minor) if major == 5 and minor is not None else None
    if not verify or not pin or not parsed:
        return HikEvent(kind='other', pin=pin, verify_mode=verify or 'none', direction='unknown', **base)
    att = re.sub(r'[^a-z]', '', (_str(ace.get('attendanceStatus')) or '').lower())
    return HikEvent(kind='attendance', pin=pin, verify_mode=verify, direction=DIRECTION.get(att, 'unknown'), **base)


def parse_multipart(body: bytes, content_type: str) -> dict[str, dict]:
    """Minimal multipart/form-data splitter: returns name -> {'headers': str, 'data': bytes} for each part."""
    parts: dict[str, dict] = {}
    bm = re.search(r'boundary="?([^";]+)"?', content_type, re.IGNORECASE)
    if not bm:
        return parts
    boundary = ('--' + bm.group(1)).encode('utf-8')
    pos = body.find(boundary)
    while pos != -1:
        start = pos + len(boundary)
        if body[start:start + 2] == b'--':        # closing boundary
            break
        nxt = body.find(boundary, start)
        segment = body[start:len(body) if nxt == -1 else nxt]
        # Header/body separator may be CRLF CRLF (spec) or LF LF (some firmware).
        header_end, sep = segment.find(b'\r\n\r\n'), 4
        if header_end == -1:
            header_end, sep = segment.find(b'\n\n'), 2
        if header_end != -1:
            headers = segment[:header_end].decode('utf-8', 'replace')
            data = segment[header_end + sep:]
            if data[-2:] == b'\r\n':
                data = data[:-2]
            elif data[-1:] == b'\n':
                data = data[:-1]
            nm = re.search(r'name="([^"]+)"', headers, re.IGNORECASE)
            if nm:
                parts[nm.group(1)] = {'headers': headers, 'data': data}
        if nxt == -1:
            break
        pos = nxt
    return parts


def _reject_constant(_name):
    raise ValueError('not JSON')


def _json(text: str):
    """JSON.parse: no NaN/Infinity literals."""
    return json.loads(text, parse_constant=_reject_constant)


def extract_event_json(body: bytes, content_type: str):
    """Extract the event JSON from a JSON or multipart request. Returns None when nothing parseable is present."""
    if re.search(r'multipart/', content_type, re.IGNORECASE):
        for name, part in parse_multipart(body, content_type).items():
            if re.search(r'picture|image', name, re.IGNORECASE) or re.search(r'image/', part['headers'], re.IGNORECASE):
                continue   # photos are discarded (plan §8.9 opt-in)
            text = js_trim(part['data'].decode('utf-8', 'replace'))
            if text.startswith('<'):
                return xml_event_to_json(text)
            try:
                return _json(text)
            except ValueError:
                pass
        return None
    text = js_trim(body.decode('utf-8', 'replace'))
    if not text:
        return None
    if text.startswith('<'):
        return xml_event_to_json(text)
    try:
        return _json(text)
    except ValueError:
        return None


XML_FIELDS = ('ipAddress', 'dateTime', 'eventType', 'serialNo', 'deviceID')
XML_ACE_FIELDS = ('deviceName', 'majorEventType', 'subEventType', 'employeeNoString', 'employeeNo', 'cardNo', 'serialNo',
                  'deviceID', 'currentVerifyMode', 'attendanceStatus')


def _xml_text(xml: str, tag: str) -> str | None:
    m = re.search(f'<{tag}(?:\\s[^>]*)?>([^<]*)</{tag}>', xml, re.IGNORECASE)
    if not m:
        return None
    v = m.group(1).replace('&amp;', '&').replace('&lt;', '<').replace('&gt;', '>').replace('&quot;', '"').replace('&apos;', "'")
    return js_trim(v)


def xml_event_to_json(xml: str) -> dict | None:
    """Older firmware posts <EventNotificationAlert> XML (Appendix C). Only the fields the attendance filter needs are
    extracted, into the same shape as the JSON payload, so one parser serves both formats."""
    if not re.search(r'<EventNotificationAlert[\s>]', xml, re.IGNORECASE):
        return None
    out: dict = {'fromXml': True}
    for f in XML_FIELDS:
        v = _xml_text(xml, f)
        if v is not None:
            out[f] = v
    m = re.search(r'<AccessControllerEvent[\s>]([\s\S]*?)</AccessControllerEvent>', xml, re.IGNORECASE)
    if m:
        ace = {}
        for f in XML_ACE_FIELDS:
            v = _xml_text(m.group(1), f)
            if v is not None:
                ace[f] = v
        out['AccessControllerEvent'] = ace
    return out
