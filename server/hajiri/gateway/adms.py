"""ZKTeco ADMS/PUSH protocol (plan Appendix B): ATTLOG parsing, command results, drift, template uploads and the
handshake options block. Port of apps/gateway/src/adms/{parse,templates,options}.ts.

ATTLOG body: one record per line, tab-separated: PIN \\t YYYY-MM-DD HH:MM:SS \\t status \\t verify [\\t workcode ...].
Status and verify code tables vary by firmware; unknown codes are kept as 'code:N' and must be verified on purchased
devices (plan §7.3).
"""
from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from urllib.parse import parse_qsl

from .jsutil import is_safe_integer, js_number, js_round, js_trim, utc_ms, utc_parts, utf16_len

_TIME_RE = re.compile(r'([0-9]{4})-([0-9]{2})-([0-9]{2}) ([0-9]{2}):([0-9]{2}):([0-9]{2})')
_INT_RE = re.compile(r'-?[0-9]{1,6}')
_LINES = re.compile(r'\r?\n')


@dataclass
class AttlogRecord:
    pin: str
    device_local_time: str
    status_code: int
    verify_code: int
    workcode: str | None
    line: str


@dataclass
class AttlogParseResult:
    records: list[AttlogRecord] = field(default_factory=list)
    malformed: list[str] = field(default_factory=list)


def is_valid_device_time(s: str) -> bool:
    """'YYYY-MM-DD HH:MM:SS' that is a real calendar time (rejects 2026-13-40 24:61:00 instead of rolling over)."""
    m = _TIME_RE.fullmatch(s)
    if not m:
        return False
    y, mo, d, h, mi, sec = (int(x) for x in m.groups())
    return is_real_datetime(y, mo, d, h, mi, sec)


def is_real_datetime(y: int, mo: int, d: int, h: int, mi: int, sec: int) -> bool:
    if mo < 1 or mo > 12 or d < 1 or d > 31 or h > 23 or mi > 59 or sec > 59:
        return False
    # Date.UTC maps years 0..99 to the 1900s, so those never match, as in the TS.
    yy, mm, dd, _ = utc_parts(utc_ms(y, mo, d, h, mi, sec))
    return yy == y and mm == mo and dd == d


def parse_attlog(body: str) -> AttlogParseResult:
    res = AttlogParseResult()
    for raw_line in _LINES.split(body):
        line = js_trim(raw_line)
        if not line:
            continue
        cols = line.split('\t')

        def col(i: int) -> str:
            return js_trim(cols[i]) if i < len(cols) else ''

        pin, t, status, verify = col(0), col(1), col(2), col(3)
        if (not pin or utf16_len(pin) > 24 or not is_valid_device_time(t) or not _INT_RE.fullmatch(status)
                or not _INT_RE.fullmatch(verify)):
            res.malformed.append(line)
            continue
        res.records.append(AttlogRecord(pin=pin, device_local_time=t, status_code=int(status), verify_code=int(verify),
                                        workcode=col(4) or None, line=line))
    return res


def direction_of(status_code: int) -> str:
    """ADMS status: 0 check-in, 1 check-out, 2 break-out, 3 break-in, 4 OT-in, 5 OT-out."""
    if status_code in (0, 3, 4):
        return 'in'
    if status_code in (1, 2, 5):
        return 'out'
    return 'unknown'


VERIFY = {0: 'password', 1: 'fingerprint', 2: 'card', 3: 'password', 4: 'card', 15: 'face', 25: 'palm'}


def verify_mode_of(verify_code: int) -> str:
    return VERIFY.get(verify_code, f'code:{verify_code}')


def _first(params: list[tuple[str, str]], name: str) -> str | None:
    for k, v in params:
        if k == name:
            return v
    return None


def parse_device_cmd_results(body: str) -> list[dict]:
    """Body of POST /iclock/devicecmd: one or more lines of ID=..&Return=..&CMD=.."""
    out = []
    for line in _LINES.split(body):
        if not js_trim(line):
            continue
        p = parse_qsl(js_trim(line).removeprefix('?'), keep_blank_values=True, errors='replace')
        id_ = js_number(_first(p, 'ID'))
        rc = js_number(_first(p, 'Return'))
        if not is_safe_integer(id_) or id_ <= 0 or not is_safe_integer(rc) or abs(rc) > 2_147_483_647:
            continue
        out.append({'id': int(id_), 'returnCode': int(rc), 'cmd': _first(p, 'CMD') or ''})
    return out


def drift_seconds(device_local_time: str, tz_offset_min: int, now_ms: float | None = None) -> int:
    """'YYYY-MM-DD HH:MM:SS' device time minus server time, in seconds (positive = device ahead)."""
    m = _TIME_RE.fullmatch(device_local_time)
    if not m:
        return 0
    now = time.time() * 1000 if now_ms is None else now_ms
    y, mo, d, h, mi, sec = (int(x) for x in m.groups())
    as_utc = utc_ms(y, mo, d, h, mi, sec) - tz_offset_min * 60_000
    return js_round((as_utc - now) / 1000)


# ------------------------------------------------------------------ templates
# Templates arrive either as OPERLOG lines prefixed FP / FACE / BIODATA, or as /iclock/fdata bodies with
# table=FINGERTMP|FACE|BIODATA. All use tab-separated key=value pairs (community sources; verify on hardware).
#   FP PIN=1\tFID=6\tSize=1234\tValid=1\tTMP=<base64>
#   FACE PIN=1\tFID=0\tSIZE=..\tVALID=1\tTMP=<base64>
#   BIODATA Pin=1\tNo=0\tIndex=0\tValid=1\tDuress=0\tType=2\tMajorVer=..\tMinorVer=..\tFormat=..\tTmp=<base64>

@dataclass
class TemplateUpload:
    pin: str
    modality: str              # fingerprint | face | palm | unknown
    vendor_format: str
    template_b64: str          # as sent by the device (never decoded or logged)
    valid: bool


@dataclass
class TemplateParseResult:
    templates: list[TemplateUpload] = field(default_factory=list)
    photos: int = 0
    unknown: int = 0
    users: int = 0


def _biodata_modality(type_: str) -> str:
    if type_ == '1':
        return 'fingerprint'
    if type_ in ('2', '9'):
        return 'face'
    if type_ in ('6', '8'):
        return 'palm'
    return 'unknown'


def _kv(line: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for part in line.split('\t'):
        i = part.find('=')
        if i > 0:
            out[js_trim(part[:i]).lower()] = js_trim(part[i + 1:])
    return out


_B64_RE = re.compile(r'[A-Za-z0-9+/=\r\n]+')
# JS '.' stops at line terminators, so a stray CR inside a line makes the prefix not match (as in the TS).
_PREFIX_RE = re.compile('(FP|FACE|BIODATA|USER|OPLOG|USERPIC|BIOPHOTO)\\s+([^\\n\\r\\u2028\\u2029]*)', re.IGNORECASE)
_WS_RE = re.compile(r'\s')


def parse_template_upload(table: str, body: str) -> TemplateParseResult:
    """Parse OPERLOG or fdata bodies for template records. `table` is the query-string table hint (may be empty)."""
    res = TemplateParseResult()
    hint = table.upper()
    if hint in ('ATTPHOTO', 'USERPIC', 'BIOPHOTO'):
        res.photos += 1
        return res
    for raw_line in _LINES.split(body):
        line = js_trim(raw_line)
        if not line:
            continue
        m = _PREFIX_RE.fullmatch(line)
        prefix = (m.group(1) if m else hint).upper()
        fields = _kv(m.group(2) if m else line)
        pin = fields.get('pin', '')
        if prefix == 'USER':
            res.users += 1
            continue
        if prefix == 'OPLOG':
            continue
        if prefix in ('USERPIC', 'BIOPHOTO'):
            res.photos += 1
            continue
        tmp = fields.get('tmp', '')
        if not pin or not tmp or not _B64_RE.fullmatch(tmp):
            res.unknown += 1
            continue
        valid = fields.get('valid', '1') != '0'
        clean = _WS_RE.sub('', tmp)
        if prefix in ('FP', 'FINGERTMP'):
            res.templates.append(TemplateUpload(pin, 'fingerprint', 'zk_fp_v10', clean, valid))
        elif prefix == 'FACE':
            res.templates.append(TemplateUpload(pin, 'face', 'zk_face_v7', clean, valid))
        elif prefix == 'BIODATA':
            t = fields.get('type', '')
            fmt = f"zk_biodata_t{t or '?'}_v{fields.get('majorver', '?')}.{fields.get('minorver', '?')}"
            res.templates.append(TemplateUpload(pin, _biodata_modality(t), fmt, clean, valid))
        else:
            res.unknown += 1
    return res


def build_options(sn: str, *, attlog_stamp: str, oplog_stamp: str, server_version: str) -> str:
    """Handshake reply for an approved device (plan Appendix B). Stamps are server-side cursors."""
    return '\n'.join([
        f'GET OPTION FROM: {sn}',
        f'Stamp={attlog_stamp}',
        f'OpStamp={oplog_stamp}',
        'PhotoStamp=0',
        f'ATTLOGStamp={attlog_stamp}',
        f'OPERLOGStamp={oplog_stamp}',
        'ATTPHOTOStamp=0',
        'ErrorDelay=60',
        'Delay=10',
        'TransTimes=00:00;14:05',
        'TransInterval=1',
        'TransFlag=1111000000',
        'Realtime=1',
        'Encrypt=0',
        f'ServerVer={server_version}',
        '',
    ])
