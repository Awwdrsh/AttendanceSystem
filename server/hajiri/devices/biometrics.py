"""
Biometric consent, template lifecycle (crypto-shredding), transparency and data-subject access (port of
apps/api/src/modules/biometrics/routes.ts; plan §6.11 Privacy Act 2075, §9.1 decisions 5-6, §12.3, §12.11, §13.13).
Ciphertext never leaves the database through this module.
"""
from __future__ import annotations

from datetime import timedelta

from django.db import transaction
from django.db.models import BooleanField, Count, Exists, ExpressionWrapper, F, OuterRef, Q, QuerySet, TextField, Value
from django.db.models.functions import Cast, Coalesce, Now

from hajiri.attendance.repo import latest_days
from hajiri.core.audit import audit
from hajiri.core.auth import AuthUser, has_role
from hajiri.core.clock import now, npt_today
from hajiri.core.errors import ForbiddenError, NotFoundError
from hajiri.core.validation import parse_uuid, plain, validate
from hajiri.db.models import (BiometricTemplate, ConsentRecord, Employee, LeaveLedger, LeaveRequest,
                              NotificationOutbox, OtRequest, PunchEvent, RegularisationRequest)

from .serializers import MODALITIES, ConsentIn, EmployeeQuery, RetentionRunIn  # noqa: F401

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


def _require(user: AuthUser, *roles: str) -> None:
    if not has_role(user, *roles):
        raise ForbiddenError(f'requires role {" or ".join(roles)}')


def _shred(templates: QuerySet) -> int:
    """Crypto-shredding: the ciphertext is emptied and the row kept as a record that it existed."""
    return templates.filter(shredded_at__isnull=True).update(ciphertext=b'', shredded_at=Now())


def _company_templates(company_id: str | None) -> QuerySet:
    """Templates of the company's employees, plus unmapped ones enrolled on the company's devices."""
    if company_id is None:
        return BiometricTemplate.objects.none()
    return BiometricTemplate.objects.filter(Q(employee__company_id=company_id) | Q(enrolled_on_device__company_id=company_id))


def _in_company(employee_id: str, company_id: str | None) -> bool:
    return Employee.objects.for_company(company_id).filter(pk=employee_id).exists()


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


def _employee_query(user: AuthUser, query: dict) -> str:
    eid = str(validate(EmployeeQuery, query)['employeeId'])
    assert_self_or_hr(user, eid)
    return eid


def transparency() -> dict:
    return TRANSPARENCY


def grant_consent(user: AuthUser, body: dict) -> dict:
    b = plain(validate(ConsentIn, body))
    assert_self_or_hr(user, b['employeeId'])
    with transaction.atomic():
        c = ConsentRecord.objects.create(employee_id=b['employeeId'], purpose=b['purpose'], modality=b['modality'],
                                         notice_version=b['noticeVersion'], signature=b.get('signature'),
                                         recorded_by_id=user.id)
        audit('consent_granted', 'consent_record', actor_id=user.id, row_id=str(c.id),
              after={'employeeId': b['employeeId'], 'modality': b['modality'], 'noticeVersion': b['noticeVersion']})
    return {'consent': {'id': str(c.id), 'grantedAt': c.granted_at}}


def consents(user: AuthUser, query: dict) -> list[dict]:
    eid = _employee_query(user, query)
    return list(ConsentRecord.objects.for_company(user.company_id).filter(employee_id=eid).order_by('-granted_at')
                .values('id', 'purpose', 'modality', 'notice_version', 'granted_at', 'revoked_at'))


def revoke_consent(user: AuthUser, consent_id) -> dict:
    """Withdrawal shreds that modality's templates immediately (plan §12.11 crypto-shredding)."""
    cid = parse_uuid(str(consent_id))
    row = (ConsentRecord.objects.for_company(user.company_id).filter(pk=cid)
           .values('employee_id', 'modality', 'revoked_at').first())
    if not row:
        raise NotFoundError('consent not found')
    emp = str(row['employee_id'])
    assert_self_or_hr(user, emp)
    with transaction.atomic():
        if not row['revoked_at']:
            ConsentRecord.objects.filter(pk=cid).update(revoked_at=Now())
        shredded = _shred(BiometricTemplate.objects.filter(employee_id=emp, modality=row['modality']))
        audit('consent_revoked', 'consent_record', actor_id=user.id, row_id=cid,
              after={'employeeId': emp, 'modality': row['modality'], 'templatesShredded': shredded})
    return {'ok': True, 'templatesShredded': shredded}


def templates(user: AuthUser, query: dict) -> list[dict]:
    """Metadata only; ciphertext is never returned."""
    eid = _employee_query(user, query)
    return list(BiometricTemplate.objects.for_company(user.company_id).filter(employee_id=eid).order_by('-created_at')
                .values('id', 'modality', 'vendor_format', 'device_user_pin', 'key_version', 'consent_id', 'created_at',
                        'shredded_at', device_sn=F('enrolled_on_device__serial_no')))


def unmapped_templates(user: AuthUser) -> list[dict]:
    """Templates uploaded for a device PIN that is not mapped yet (so HR can map or shred them)."""
    _require(user, 'hr')
    return list(BiometricTemplate.objects
                .filter(employee__isnull=True, shredded_at__isnull=True, enrolled_on_device__company_id=user.company_id)
                .order_by('-created_at')
                .values('id', 'modality', 'vendor_format', 'device_user_pin', 'created_at',
                        device_sn=F('enrolled_on_device__serial_no'))[:500])


def shred_template(user: AuthUser, template_id) -> dict:
    _require(user, 'hr')
    tid = parse_uuid(str(template_id))
    t = _company_templates(user.company_id).filter(pk=tid).values('employee_id').first()
    if not t:
        raise NotFoundError('template not found')
    with transaction.atomic():
        n = _shred(BiometricTemplate.objects.filter(pk=tid))
        audit('template_shred', 'biometric_template', actor_id=user.id, row_id=tid,
              after={'employeeId': t['employee_id'] and str(t['employee_id'])})
    return {'ok': True, 'shredded': n}


def shred_all(user: AuthUser, employee_id) -> dict:
    _require(user, 'hr')
    eid = parse_uuid(str(employee_id))
    if not _in_company(eid, user.company_id):
        raise NotFoundError('employee not found')
    with transaction.atomic():
        n = _shred(BiometricTemplate.objects.filter(employee_id=eid))
        ConsentRecord.objects.filter(employee_id=eid).update(revoked_at=Coalesce('revoked_at', Now()))
        audit('template_shred_all', 'biometric_template', actor_id=user.id, row_id=eid, after={'shredded': n})
    return {'ok': True, 'shredded': n}


DSAR_EMPLOYEE_FIELDS = (
    'id', 'emp_code', 'name_en', 'name_ne', 'gender', 'dob', 'marital_status', 'join_date', 'exit_date', 'designation',
    'grade', 'phone', 'email', 'ot_eligible', 'weekly_off_days', 'status', 'regime', 'ssf_id', 'pf_id', 'cit_id',
    'bank_code', 'bank_branch', 'bank_account_last4', 'is_resident', 'remote_area_class', 'disability')


def _requests(eid: str) -> list[dict]:
    """Every leave, correction and overtime request of the employee, oldest first: one UNION ALL query. Every part
    selects the same columns in the same order (status, reason, created_at, then the annotations kind, from, to)."""
    def part(model, kind, start, end):
        return model.objects.filter(employee_id=eid).values(
            'status', 'reason', 'created_at', kind_t=Value(kind, output_field=TextField()),
            from_t=Cast(start, TextField()), to_t=Cast(end, TextField()))
    union = part(LeaveRequest, 'leave', 'from_date', 'to_date').union(
        part(RegularisationRequest, 'regularisation', 'local_date', 'local_date'),
        part(OtRequest, 'overtime', 'local_date', 'local_date'), all=True).order_by('created_at')
    return [{'kind': r['kind_t'], 'from_date': r['from_t'], 'to_date': r['to_t'], 'status': r['status'],
             'reason': r['reason'], 'created_at': r['created_at']} for r in union]


def dsar(user: AuthUser, query: dict) -> dict:
    """Data-subject access request export: the employee's own data, nothing else (plan §12.11 DSAR)."""
    eid = _employee_query(user, query)
    emp = (Employee.objects.for_company(user.company_id).filter(pk=eid)
           .values(*DSAR_EMPLOYEE_FIELDS, has_pan=ExpressionWrapper(Q(pan_enc__isnull=False), output_field=BooleanField()))
           .first())
    if not emp:
        raise NotFoundError('employee not found')
    # The employee was checked to be in the active company above, so their own rows below are this company's.
    out = {
        'generatedAt': now(),
        'employee': emp,
        'consents': list(ConsentRecord.objects.filter(employee_id=eid).order_by('granted_at')
                         .values('id', 'purpose', 'modality', 'notice_version', 'granted_at', 'revoked_at')),
        'templates': list(BiometricTemplate.objects.filter(employee_id=eid).order_by('created_at')
                          .values('id', 'modality', 'vendor_format', 'device_user_pin', 'created_at', 'shredded_at')),
        'attendance': list(latest_days().filter(employee_id=eid).order_by('local_date')
                           .values('local_date', 'bs_date', 'status', 'first_in', 'last_out', 'worked_min', 'late_min',
                                   'early_min', 'ot_min', 'flags')),
        'punches': list(PunchEvent.objects.filter(employee_id=eid).order_by('punched_at')
                        .values('punched_at', 'local_date', 'direction', 'verify_mode', 'source', 'device_sn')),
        'leaveLedger': list(LeaveLedger.objects.filter(employee_id=eid).order_by('effective_date')
                            .values('effective_date', 'delta_days', 'reason', 'note', 'leave_type_id')),
        'requests': _requests(eid),
        'notifications': list(NotificationOutbox.objects.filter(employee_id=eid).order_by('created_at')
                              .values('created_at', 'channel', 'template', 'title', 'body')),
        'retention': RETENTION,
    }
    audit('dsar_export', 'employee', actor_id=user.id, row_id=eid)
    return out


def run_retention(user: AuthUser | None, body: dict | None = None, *, company_id: str | None = None) -> dict:
    """Retention schedule (plan §12.11): shred templates 30 days after exit; attendance-photo hook for when photos exist."""
    if user is not None:
        _require(user, 'hr', 'admin')
        company_id = user.company_id
    b = validate(RetentionRunIn, body or {})
    cutoff = npt_today() - timedelta(days=b['templateGraceDays'])
    live = BiometricTemplate.objects.filter(employee=OuterRef('pk'), shredded_at__isnull=True)
    exited = list(Employee.objects.for_company(company_id).filter(exit_date__isnull=False, exit_date__lt=cutoff)
                  .filter(Exists(live)).values_list('id', flat=True))
    with transaction.atomic():
        shredded = _shred(BiometricTemplate.objects.filter(employee_id__in=exited)) if exited else 0
        audit('retention_run', 'biometric_template', actor_id=user.id if user else None,
              after={'employees': len(exited), 'shredded': shredded, 'photosDeleted': 0})
    return {'employees': len(exited), 'templatesShredded': shredded, 'photosDeleted': 0}


def stats(user: AuthUser) -> list[dict]:
    _require(user, 'hr')
    live = Q(shredded_at__isnull=True)
    return list(_company_templates(user.company_id).values('modality')
                .annotate(active=Count('id', filter=live), shredded=Count('id', filter=~live),
                          unmapped=Count('id', filter=live & Q(employee__isnull=True)))
                .order_by('modality'))
