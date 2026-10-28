"""
Profile change requests (port of apps/api/src/modules/people/change-requests.ts). An employee proposes a new value for
one profile section (optionally with proof); HR approves it through the approval inbox and it is applied with field
history. With people.policy.makerChecker on, HR's own edits of bank and ID details become requests too, and a
different HR person must approve them.
"""
from __future__ import annotations

import json

from django.db.models import F
from django.db.models.functions import Now

from hajiri.core.crypto import field_cipher
from hajiri.core.errors import ConflictError, ForbiddenError, NotFoundError
from hajiri.db.models import Employee, ProfileChangeRequest
from hajiri.notifications.service import notify

from .accounts import user_id_for_employee
from .profile import SECTION_LABEL, apply_section, diff_section

PROFILE_CHANGE = 'profile_change'
REQ_COLS = ('id', 'company_id', 'employee_id', 'requested_by', 'section', 'summary', 'reason', 'proof_file_id', 'status',
            'decided_by', 'decided_at', 'decision_reason', 'created_at')
Status = ProfileChangeRequest.Status


def _aad(company_id: str, employee_id: str) -> str:
    return f'profile_change:{company_id}:{employee_id}'


def req_values(qs):
    """A ProfileChangeRequest queryset as rows with the TS column names (.values() gives foreign keys as ids)."""
    return qs.values(*REQ_COLS)


def req_row(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in ('id', 'company_id', 'employee_id', 'requested_by', 'proof_file_id', 'decided_by', 'instance_id'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    out['summary'] = out.get('summary') or {}
    return out


def decrypt_payload(row) -> dict:
    get = (lambda k: row[k]) if isinstance(row, dict) else (lambda k: getattr(row, k))
    return json.loads(field_cipher().decrypt(bytes(get('payload_enc')), _aad(str(get('company_id')), str(get('employee_id')))))


def create_change_request(*, company_id: str, employee_id: str, requested_by: str, section: str, value: dict,
                          reason: str | None = None, proof_file_id: str | None = None, by_hr: bool) -> dict:
    from hajiri.workflow.services import start_workflow
    changes = diff_section(employee_id, section, value)
    if not changes:
        raise ConflictError('nothing would change; the profile already has these details')
    if ProfileChangeRequest.objects.for_company(company_id).filter(employee_id=employee_id, section=section,
                                                                    status=Status.PENDING).exists():
        raise ConflictError(f'a change to {SECTION_LABEL[section].lower()} is already waiting for approval')
    created = ProfileChangeRequest.objects.create(
        company_id=company_id, employee_id=employee_id, requested_by_id=requested_by, section=section,
        payload_enc=field_cipher().encrypt(json.dumps(value, ensure_ascii=False), _aad(company_id, str(employee_id))),
        summary={'changes': changes, 'byHr': by_hr}, reason=reason, proof_file_id=proof_file_id)
    row = req_row(req_values(ProfileChangeRequest.objects.filter(pk=created.pk)).get())
    name = Employee.objects.filter(pk=employee_id).values_list('name_en', flat=True).first()
    start_workflow(company_id=company_id, object_type=PROFILE_CHANGE, object_id=row['id'], employee_id=str(employee_id),
                   title='Second approval needed' if by_hr else 'Profile change to review',
                   body=f"{name or 'An employee'}: {SECTION_LABEL[section].lower()} ({', '.join(c['label'] for c in changes)})")
    return row


def decided(object_id: str, state: str, ctx) -> None:
    """Workflow decision handler for profile changes (runs inside the decision's transaction)."""
    row = ProfileChangeRequest.objects.select_for_update().filter(pk=object_id).first()
    if not row:
        raise NotFoundError('change request not found')
    if row.status != Status.PENDING:
        raise ConflictError(f'this change request is already {row.status}')
    # Maker-checker: whoever proposed a change can never be the one who approves it.
    if state == 'approved' and str(row.requested_by_id) == str(ctx.actor_id):
        raise ForbiddenError('a different HR person must approve this change')
    if state == 'approved':
        apply_section(company_id=str(row.company_id), employee_id=str(row.employee_id), section=row.section,
                      value=decrypt_payload(row), actor_id=ctx.actor_id, source='change_request', ref_id=str(row.id))
    ProfileChangeRequest.objects.filter(pk=row.pk).update(status=state, decided_by_id=ctx.actor_id, decided_at=Now(),
                                                          decision_reason=ctx.reason)
    recipients = [str(row.requested_by_id)]
    emp_user = user_id_for_employee(str(row.employee_id))
    if emp_user and emp_user not in recipients:
        recipients.append(emp_user)
    label = SECTION_LABEL[row.section]
    if state == 'rejected' and ctx.reason:
        body = f'{label}: {ctx.reason}'
    elif state == 'approved':
        body = f'{label} updated.'
    else:
        body = f'{label}.'
    for user_id in recipients:
        if user_id == str(ctx.actor_id):
            continue
        notify(company_id=str(row.company_id), user_id=user_id, template=f'{PROFILE_CHANGE}.{state}',
               title='Profile change approved' if state == 'approved' else 'Profile change not approved', body=body,
               payload={'requestId': str(row.id)})


def summarise(inst: dict) -> dict:
    row = (ProfileChangeRequest.objects.filter(pk=inst['object_id'])
           .values('section', 'summary', 'reason', 'proof_file_id', requested_by_name=F('requested_by__name')).first())
    if not row:
        return {}
    summary = row['summary'] or {}
    return {
        'section': row['section'], 'sectionLabel': SECTION_LABEL[row['section']], 'changes': summary.get('changes') or [],
        'reason': row['reason'], 'hasProof': bool(row['proof_file_id']), 'requestedBy': row['requested_by_name'],
        'byHr': bool(summary.get('byHr')),
    }


def register() -> None:
    """Wire the decision handler and the inbox summary (called from PeopleConfig.ready)."""
    from hajiri.workflow.services import register_handler, register_summariser
    register_handler(PROFILE_CHANGE, decided)
    register_summariser(PROFILE_CHANGE, summarise)
