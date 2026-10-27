"""
Biometric consent, template lifecycle (crypto-shredding), transparency and data-subject access (port of
apps/api/src/modules/biometrics/routes.ts; plan §6.11 Privacy Act 2075, §9.1 decisions 5-6, §12.3, §12.11, §13.13).
Ciphertext never leaves the database through this module.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction

from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_role
from hajiri.core.clock import now, npt_today
from hajiri.core.db import execute, one, rows
from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.core.schema import Enum, Int, Obj, Str, Uuid, parse

MODALITIES = ('face', 'fingerprint', 'palm', 'card')

RETENTION = {
    'templates': {'en': 'Biometric templates: until 30 days after exit, then cryptographically shredded', 'ne': 'बायोमेट्रिक टेम्प्लेट: बहिर्गमनको ३० दिन पछिसम्म, त्यसपछि नष्ट'},
    'photos': {'en': 'Attendance photos: 90 days (only on devices where photo capture is enabled)', 'ne': 'हाजिरी फोटो: ९० दिन (फोटो खिच्ने सुविधा सक्षम भएका यन्त्रमा मात्र)'},
    'attendance': {'en': 'Attendance records: 5 years', 'ne': 'हाजिरी अभिलेख: ५ वर्ष'},
    'payroll': {'en': 'Payroll records: 7 years', 'ne': 'तलब अभिलेख: ७ वर्ष'},
}

TRANSPARENCY = {
    'stored': {
        'en': ['Face, fingerprint or palm template produced by the terminal (encrypted; the original image is not kept)', 'Punch time, device and verification method', 'Location only at the moment of a mobile punch', 'A photo when you clock in from home (encrypted; seen only by you, your manager and HR; deleted automatically after the retention period)'],
        'ne': ['यन्त्रले बनाएको अनुहार, औंठाछाप वा हत्केलाको टेम्प्लेट (इन्क्रिप्ट गरिएको; मूल तस्बिर राखिँदैन)', 'हाजिरीको समय, यन्त्र र प्रमाणीकरण विधि', 'मोबाइल हाजिरीको क्षणमा मात्र स्थान', 'घरबाट हाजिरी गर्दा खिचिएको तस्बिर (इन्क्रिप्ट गरिएको; तपाईं, प्रबन्धक र मानव संसाधनले मात्र हेर्न सक्ने; अवधि सकिएपछि स्वतः मेटिने)'],
    },
    'notStored': {'en': ['Continuous location tracking', 'Productivity scores or lateness rankings'], 'ne': ['निरन्तर स्थान ट्र्याकिङ', 'उत्पादकता अंक वा ढिलाइको श्रेणी']},
    'retention': RETENTION,
    'access': {
        'en': ['You: your own punches, attendance, corrections and this record', 'Your manager: your attendance status, requests and work-from-home photos', 'HR: your profile, attendance and payroll; sensitive fields are decrypted on demand and every read is logged'],
        'ne': ['तपाईं: आफ्नै हाजिरी, सुधार र यो अभिलेख', 'तपाईंको प्रबन्धक: तपाईंको हाजिरी स्थिति र अनुरोध', 'मानव संसाधन: प्रोफाइल र हाजिरी; संवेदनशील क्षेत्र आवश्यक पर्दा मात्र खोलिन्छ र हरेक पहुँच लग हुन्छ'],
    },
    'withdraw': {
        'en': 'You may withdraw biometric consent at any time from My data; your templates are shredded and you punch with card or PIN instead. Contact HR to correct any record (Privacy Act 2075 s.28).',
        'ne': 'तपाईंले जुनसुकै बेला "मेरो डेटा" बाट बायोमेट्रिक सहमति फिर्ता लिन सक्नुहुन्छ; टेम्प्लेट नष्ट गरिन्छ र कार्ड वा पिनबाट हाजिरी गर्न सकिन्छ। अभिलेख सच्याउन मानव संसाधनमा सम्पर्क गर्नुहोस् (गोपनीयता ऐन २०७५ दफा २८)।',
    },
    'legalBasis': {'en': 'Privacy Act 2075 s.23 (consent and notice), s.25 (security), s.28 (correction)', 'ne': 'गोपनीयता ऐन २०७५ दफा २३, २५, २८'},
}

CONSENT = Obj({'employeeId': Uuid(), 'purpose': Str(min=1, max=200, default='attendance'), 'modality': Enum(MODALITIES),
               'noticeVersion': Str(min=1, max=40), 'signature': Str(max=4000, optional=True)})
EMPLOYEE_QUERY = Obj({'employeeId': Uuid()})
RETENTION_RUN = Obj({'templateGraceDays': Int(min=0, max=365, default=30)})


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


def _shred(where: str, params: list) -> int:
    return execute(f"UPDATE biometric_template SET ciphertext = '\\x'::bytea, shredded_at = now() WHERE shredded_at IS NULL AND {where}", params)


def _in_company(employee_id: str, company_id: str | None) -> bool:
    return one('SELECT 1 AS x FROM employee WHERE id = %s AND company_id = %s', [employee_id, company_id]) is not None


def assert_self_or_hr(user: AuthUser, employee_id: str) -> None:
    """
    Consent, templates and data exports are the employee's own business or HR's: managers may see attendance, never
    biometric records (plan §12.11 data minimisation). Cross-company ids are indistinguishable from missing ones (404).
    """
    if not _in_company(employee_id, user.company_id):
        raise NotFoundError('employee not found')
    if has_role(user, 'hr') or user.employee_id == str(employee_id):
        return
    raise ForbiddenError('not allowed to access this employee')


def transparency() -> dict:
    return TRANSPARENCY


def grant_consent(user: AuthUser, body: dict) -> dict:
    b = parse(CONSENT, body)
    assert_self_or_hr(user, b['employeeId'])
    with transaction.atomic():
        r = one("""INSERT INTO consent_record (employee_id, purpose, modality, notice_version, signature, recorded_by)
                   VALUES (%s, %s, %s, %s, %s, %s) RETURNING id, granted_at""",
                [b['employeeId'], b['purpose'], b['modality'], b['noticeVersion'], b.get('signature'), user.id])
        audit('consent_granted', 'consent_record', actor_id=user.id, row_id=str(r['id']),
              after={'employeeId': b['employeeId'], 'modality': b['modality'], 'noticeVersion': b['noticeVersion']})
    return {'consent': {'id': str(r['id']), 'grantedAt': r['granted_at']}}


def consents(user: AuthUser, query: dict) -> list[dict]:
    q = parse(EMPLOYEE_QUERY, query)
    assert_self_or_hr(user, q['employeeId'])
    return rows("""SELECT c.id, c.purpose, c.modality, c.notice_version, c.granted_at, c.revoked_at
                     FROM consent_record c JOIN employee e ON e.id = c.employee_id
                    WHERE c.employee_id = %s AND e.company_id = %s ORDER BY c.granted_at DESC""", [q['employeeId'], user.company_id])


def revoke_consent(user: AuthUser, consent_id) -> dict:
    """Withdrawal shreds that modality's templates immediately (plan §12.11 crypto-shredding)."""
    cid = parse(Uuid(), str(consent_id))
    row = one("""SELECT c.employee_id, c.modality, c.revoked_at FROM consent_record c JOIN employee e ON e.id = c.employee_id
                  WHERE c.id = %s AND e.company_id = %s""", [cid, user.company_id])
    if not row:
        raise NotFoundError('consent not found')
    emp = str(row['employee_id'])
    assert_self_or_hr(user, emp)
    with transaction.atomic():
        if not row['revoked_at']:
            execute('UPDATE consent_record SET revoked_at = now() WHERE id = %s', [cid])
        shredded = _shred('employee_id = %s AND modality = %s', [emp, row['modality']])
        audit('consent_revoked', 'consent_record', actor_id=user.id, row_id=cid,
              after={'employeeId': emp, 'modality': row['modality'], 'templatesShredded': shredded})
    return {'ok': True, 'templatesShredded': shredded}


def templates(user: AuthUser, query: dict) -> list[dict]:
    """Metadata only; ciphertext is never returned."""
    q = parse(EMPLOYEE_QUERY, query)
    assert_self_or_hr(user, q['employeeId'])
    return rows("""SELECT t.id, t.modality, t.vendor_format, t.device_user_pin, d.serial_no AS device_sn, t.key_version, t.consent_id,
                          t.created_at, t.shredded_at
                     FROM biometric_template t JOIN employee e ON e.id = t.employee_id LEFT JOIN device d ON d.id = t.enrolled_on_device_id
                    WHERE t.employee_id = %s AND e.company_id = %s ORDER BY t.created_at DESC""", [q['employeeId'], user.company_id])


def unmapped_templates(user: AuthUser) -> list[dict]:
    """Templates uploaded for a device PIN that is not mapped yet (so HR can map or shred them)."""
    _require(user, 'hr')
    return rows("""SELECT t.id, t.modality, t.vendor_format, t.device_user_pin, d.serial_no AS device_sn, t.created_at
                     FROM biometric_template t JOIN device d ON d.id = t.enrolled_on_device_id
                    WHERE t.employee_id IS NULL AND t.shredded_at IS NULL AND d.company_id = %s ORDER BY t.created_at DESC LIMIT 500""",
                [user.company_id])


def shred_template(user: AuthUser, template_id) -> dict:
    _require(user, 'hr')
    tid = parse(Uuid(), str(template_id))
    t = one("""SELECT t.employee_id FROM biometric_template t LEFT JOIN employee e ON e.id = t.employee_id
                 LEFT JOIN device d ON d.id = t.enrolled_on_device_id
                WHERE t.id = %s AND (e.company_id = %s OR d.company_id = %s)""", [tid, user.company_id, user.company_id])
    if not t:
        raise NotFoundError('template not found')
    with transaction.atomic():
        n = _shred('id = %s', [tid])
        audit('template_shred', 'biometric_template', actor_id=user.id, row_id=tid,
              after={'employeeId': t['employee_id'] and str(t['employee_id'])})
    return {'ok': True, 'shredded': n}


def shred_all(user: AuthUser, employee_id) -> dict:
    _require(user, 'hr')
    eid = parse(Uuid(), str(employee_id))
    if not _in_company(eid, user.company_id):
        raise NotFoundError('employee not found')
    with transaction.atomic():
        n = _shred('employee_id = %s', [eid])
        execute('UPDATE consent_record SET revoked_at = coalesce(revoked_at, now()) WHERE employee_id = %s', [eid])
        audit('template_shred_all', 'biometric_template', actor_id=user.id, row_id=eid, after={'shredded': n})
    return {'ok': True, 'shredded': n}


def dsar(user: AuthUser, query: dict) -> dict:
    """Data-subject access request export: the employee's own data, nothing else (plan §12.11 DSAR)."""
    q = parse(EMPLOYEE_QUERY, query)
    eid = q['employeeId']
    assert_self_or_hr(user, eid)
    emp = one("""SELECT id, emp_code, name_en, name_ne, gender, dob, marital_status, join_date, exit_date, designation, grade, phone, email,
                        ot_eligible, weekly_off_days, status, regime, ssf_id, pf_id, cit_id, bank_code, bank_branch, bank_account_last4,
                        is_resident, remote_area_class, disability, (pan_enc IS NOT NULL) AS has_pan
                   FROM employee WHERE id = %s AND company_id = %s""", [eid, user.company_id])
    if not emp:
        raise NotFoundError('employee not found')
    out = {
        'generatedAt': now(),
        'employee': emp,
        'consents': rows('SELECT id, purpose, modality, notice_version, granted_at, revoked_at FROM consent_record WHERE employee_id = %s ORDER BY granted_at', [eid]),
        'templates': rows('SELECT id, modality, vendor_format, device_user_pin, created_at, shredded_at FROM biometric_template WHERE employee_id = %s ORDER BY created_at', [eid]),
        'attendance': rows('SELECT local_date, bs_date, status, first_in, last_out, worked_min, late_min, early_min, ot_min, flags FROM v_attendance_latest WHERE employee_id = %s ORDER BY local_date', [eid]),
        'punches': rows('SELECT punched_at, local_date, direction, verify_mode, source, device_sn FROM punch_event WHERE employee_id = %s ORDER BY punched_at', [eid]),
        'leaveLedger': rows('SELECT effective_date, delta_days, reason, note, leave_type_id FROM leave_ledger WHERE employee_id = %s ORDER BY effective_date', [eid]),
        'requests': rows("""SELECT 'leave' AS kind, from_date::text AS from_date, to_date::text AS to_date, status, reason, created_at FROM leave_request WHERE employee_id = %(e)s
                            UNION ALL SELECT 'regularisation', local_date::text, local_date::text, status, reason, created_at FROM regularisation_request WHERE employee_id = %(e)s
                            UNION ALL SELECT 'overtime', local_date::text, local_date::text, status, reason, created_at FROM ot_request WHERE employee_id = %(e)s
                            ORDER BY created_at""", {'e': eid}),
        'notifications': rows('SELECT created_at, channel, template, title, body FROM notification_outbox WHERE employee_id = %s ORDER BY created_at', [eid]),
        'retention': RETENTION,
    }
    audit('dsar_export', 'employee', actor_id=user.id, row_id=eid)
    return out


def run_retention(user: AuthUser | None, body: dict | None = None, *, company_id: str | None = None) -> dict:
    """Retention schedule (plan §12.11): shred templates 30 days after exit; attendance-photo hook for when photos exist."""
    if user is not None:
        _require(user, 'hr', 'admin')
        company_id = user.company_id
    b = parse(RETENTION_RUN, body or {})
    cutoff = npt_today() - timedelta(days=b['templateGraceDays'])
    exited = rows("""SELECT e.id FROM employee e WHERE e.company_id = %s AND e.exit_date IS NOT NULL AND e.exit_date < %s::date
                        AND EXISTS (SELECT 1 FROM biometric_template t WHERE t.employee_id = e.id AND t.shredded_at IS NULL)""",
                  [company_id, cutoff])
    with transaction.atomic():
        shredded = 0
        for e in exited:
            shredded += _shred('employee_id = %s', [e['id']])
        audit('retention_run', 'biometric_template', actor_id=user.id if user else None,
              after={'employees': len(exited), 'shredded': shredded, 'photosDeleted': 0})
    return {'employees': len(exited), 'templatesShredded': shredded, 'photosDeleted': 0}


def stats(user: AuthUser) -> list[dict]:
    _require(user, 'hr')
    return rows("""SELECT t.modality, count(*) FILTER (WHERE t.shredded_at IS NULL)::int AS active,
                          count(*) FILTER (WHERE t.shredded_at IS NOT NULL)::int AS shredded,
                          count(*) FILTER (WHERE t.employee_id IS NULL AND t.shredded_at IS NULL)::int AS unmapped
                     FROM biometric_template t LEFT JOIN employee e ON e.id = t.employee_id LEFT JOIN device d ON d.id = t.enrolled_on_device_id
                    WHERE e.company_id = %s OR d.company_id = %s GROUP BY t.modality ORDER BY t.modality""",
                [user.company_id, user.company_id])
