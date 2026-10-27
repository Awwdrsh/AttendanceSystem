"""Terminal simulator (plan §15.3). Port of apps/gateway/src/simulate.ts. ZKTeco ADMS (default) or Hikvision ISAPI.

    manage.py gateway_simulate --url http://localhost:8080 --sn DEMO-01 --pin 1 --time "2026-09-21 10:17:03"
    manage.py gateway_simulate --vendor hikvision --sn HIK-01 --pin 1 --time "2026-09-21 10:17:03"
Repeat --pin/--time pairs to send several records; --poll N polls getrequest N times (ADMS only);
--template sends a fake fingerprint template line in an OPERLOG upload (ADMS only).
"""
from __future__ import annotations

import base64
import json
import re
import sys
import time
import urllib.error
import urllib.request
from typing import Callable
from urllib.parse import quote

from .jsutil import iso_ms

Fetch = Callable[[str, str, dict, bytes | None], tuple[int, str]]


def http_fetch(method: str, url: str, headers: dict, body: bytes | None) -> tuple[int, str]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, r.read().decode('utf-8', 'replace')
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode('utf-8', 'replace')


def _enc(s: str) -> str:
    """encodeURIComponent."""
    return quote(s, safe="-_.!~*'()")


def simulate(args: list[str], fetch: Fetch = http_fetch, out=None) -> None:
    out = out or sys.stdout

    def opt(name: str, default: str | None = None) -> str | None:
        if f'--{name}' in args:
            i = args.index(f'--{name}')
            return args[i + 1] if i + 1 < len(args) else default
        return default

    def all_of(name: str) -> list[str]:
        return [args[i + 1] if i + 1 < len(args) else '' for i, a in enumerate(args) if a == f'--{name}']

    url = opt('url', 'http://localhost:8080')
    sn = opt('sn', 'DEMO-01')
    vendor = opt('vendor', 'zkteco')
    pins, times = all_of('pin'), all_of('time')
    polls = int(float(opt('poll', '1')))
    verify = opt('verify', '1')
    status = opt('status', '0')

    def log(label: str, r: tuple[int, str]) -> None:
        out.write(f'{label} -> {r[0]}\n{r[1].rstrip()}\n\n')

    def time_at(i: int) -> str:
        return times[i] if i < len(times) else (times[0] if times else '2026-09-21 10:00:00')

    if vendor == 'hikvision':
        target = f'{url}/hikvision/event?sn={_enc(sn)}'
        hdr = {'Content-Type': 'application/json'}
        hb = {'ipAddress': '10.0.0.5', 'dateTime': iso_ms(int(time.time() * 1000)), 'eventType': 'heartBeat'}
        log('heartbeat', fetch('POST', target, hdr, json.dumps(hb).encode()))
        for i, pin in enumerate(pins):
            t = time_at(i).replace(' ', 'T', 1) + '+05:45'
            ev = {'ipAddress': '10.0.0.5', 'dateTime': t, 'eventType': 'AccessControllerEvent', 'eventState': 'active',
                  'AccessControllerEvent': {'deviceName': 'Front door', 'majorEventType': 5, 'subEventType': 75,
                                            'employeeNoString': pin, 'serialNo': sn, 'currentVerifyMode': 'face',
                                            'attendanceStatus': 'checkIn'}}
            log(f'event pin {pin}', fetch('POST', target, hdr, json.dumps(ev).encode()))
        return

    log('handshake', fetch('GET', f'{url}/iclock/cdata?SN={_enc(sn)}&options=all&pushver=2.4.1&language=69', {}, None))
    text = {'Content-Type': 'text/plain'}
    if pins:
        lines = '\n'.join(f'{pin}\t{time_at(i)}\t{status}\t{verify}\t0' for i, pin in enumerate(pins)) + '\n'
        r = fetch('POST', f'{url}/iclock/cdata?SN={_enc(sn)}&table=ATTLOG&Stamp={int(time.time() * 1000)}', text, lines.encode())
        log(f'ATTLOG ({len(pins)} record(s))', r)

    if '--template' in args:
        fake = base64.b64encode(f'simulated-template-{int(time.time() * 1000)}'.encode()).decode()
        body = f"FP PIN={pins[0] if pins else '1'}\tFID=6\tSize={len(fake)}\tValid=1\tTMP={fake}\n"
        r = fetch('POST', f'{url}/iclock/cdata?SN={_enc(sn)}&table=OPERLOG&OpStamp={int(time.time() * 1000)}', text, body.encode())
        log('OPERLOG template', r)

    for _ in range(polls):
        r = fetch('GET', f'{url}/iclock/getrequest?SN={_enc(sn)}', {}, None)
        log('getrequest', r)
        for line in r[1].split('\n'):
            m = re.fullmatch(r'C:([0-9]+):(.*)', line)
            if m:
                cmd = m.group(2).split(' ')[0]
                dc = fetch('POST', f'{url}/iclock/devicecmd?SN={_enc(sn)}', text,
                           f'ID={m.group(1)}&Return=0&CMD={_enc(cmd)}'.encode())
                log(f'devicecmd ack {m.group(1)}', dc)
