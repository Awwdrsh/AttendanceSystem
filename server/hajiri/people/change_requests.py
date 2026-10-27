"""
Profile change requests (port of apps/api/src/modules/people/change-requests.ts). An employee proposes a new value for
one profile section (optionally with proof); HR approves it through the approval inbox and it is applied with field
history. With people.policy.makerChecker on, HR's own edits of bank and ID details become requests too, and a
different HR person must approve them.
"""
from __future__ import annotations

import json

from hajiri.core.crypto import field_cipher
from hajiri.core.db import execute, one
from hajiri.core.errors import ConflictError, ForbiddenError, NotFoundError
from hajiri.notifications.service import notify, user_id_for_employee

from .profile import SECTION_LABEL, apply_section, diff_section

PROFILE_CHANGE = 'profile_change'
REQ_COLS = ('id', 'company_id', 'employee_id', 'requested_by', 'section', 'summary', 'reason', 'proof_file_id', 'status',
            'decided_by', 'decided_at', 'decision_reason', 'created_at')


def _aad(company_id: str, employee_id: str) -> str:
    return f'profile_change:{company_id}:{employee_id}'


def req_row(r: dict | None) -> dict | None:
    if r is None:
        return None
    out = dict(r)
    for k in ('id', 'company_id', 'employee_id', 'requested_by', 'proof_file_id', 'decided_by', 'instance_id'):
        if out.get(k) is not None:
            out[k] = str(out[k])
    out['summary'] = out.get('summary') or {}
    return out


def decrypt_payload(row: dict) -> dict:
    return json.loads(field_cipher().decrypt(bytes(row['payload_enc']), _aad(str(row['company_id']), str(row['employee_id']))))


def create_change_request(*, company_id: str, employee_id: str, requested_by: str, section: str, value: dict,
                          reason: str | None = None, proof_file_id: str | None = None, by_hr: bool) -> dict:
    from hajiri.workflow.services import start_workflow
    changes = diff_section(employee_id, section, value)
    if not changes:
        raise ConflictError('nothing would change; the profile already has these details')
    if one("SELECT 1 AS x FROM profile_change_request WHERE employee_id = %s AND section = %s AND status = 'pending'", [employee_id, section]):
        raise ConflictError(f'a change to {SECTION_LABEL[section].lower()} is already waiting for approval')
    row = req_row(one(f"""INSERT INTO profile_change_request (company_id, employee_id, requested_by, section, payload_enc, summary, reason, proof_file_id)
                          VALUES (%s, %s, %s, %s, %s, %s, %s, %s) RETURNING {', '.join(REQ_COLS)}""",
                      [company_id, employee_id, requested_by, section,
                       field_cipher().encrypt(json.dumps(value, ensure_ascii=False), _aad(company_id, str(employee_id))),
                       json.dumps({'changes': changes, 'byHr': by_hr}, ensure_ascii=False), reason, proof_file_id]))
    emp = one('SELECT name_en FROM employee WHERE id = %s', [employee_id])
    start_workflow(company_id=company_id, object_type=PROFILE_CHANGE, object_id=row['id'], employee_id=str(employee_id),
                   title='Second approval needed' if by_hr else 'Profile change to review',
                   body=f"{emp['name_en'] if emp else 'An employee'}: {SECTION_LABEL[section].lower()} ({', '.join(c['label'] for c in changes)})")
    return row


def decided(object_id: str, state: str, ctx) -> None:
    """Workflow decision handler for profile changes (runs inside the decision's transaction)."""
    row = one('SELECT * FROM profile_change_request WHERE id = %s FOR UPDATE', [object_id])
    if not row:
        raise NotFoundError('change request not found')
    if row['status'] != 'pending':
        raise ConflictError(f"this change request is already {row['status']}")
    # Maker-checker: whoever proposed a change can never be the one who approves it.
    if state == 'approved' and str(row['requested_by']) == str(ctx.actor_id):
        raise ForbiddenError('a different HR person must approve this change')
    if state == 'approved':
        apply_section(company_id=str(row['company_id']), employee_id=str(row['employee_id']), section=row['section'],
                      value=decrypt_payload(row), actor_id=ctx.actor_id, source='change_request', ref_id=str(row['id']))
    execute('UPDATE profile_change_request SET status = %s, decided_by = %s, decided_at = now(), decision_reason = %s WHERE id = %s',
            [state, ctx.actor_id, ctx.reason, row['id']])
    recipients = [str(row['requested_by'])]
    emp_user = user_id_for_employee(str(row['employee_id']))
    if emp_user and emp_user not in recipients:
        recipients.append(emp_user)
    label = SECTION_LABEL[row['section']]
    if state == 'rejected' and ctx.reason:
        body = f'{label}: {ctx.reason}'
    elif state == 'approved':
        body = f'{label} updated.'
    else:
        body = f'{label}.'
    for user_id in recipients:
        if user_id == str(ctx.actor_id):
            continue
        notify(company_id=str(row['company_id']), user_id=user_id, template=f'{PROFILE_CHANGE}.{state}',
               title='Profile change approved' if state == 'approved' else 'Profile change not approved', body=body,
               payload={'requestId': str(row['id'])})


def summarise(inst: dict) -> dict:
    row = one('SELECT section, summary, reason, proof_file_id, requested_by FROM profile_change_request WHERE id = %s', [inst['object_id']])
    if not row:
        return {}
    by = one('SELECT name FROM app_user WHERE id = %s', [row['requested_by']])
    summary = row['summary'] or {}
    return {
        'section': row['section'], 'sectionLabel': SECTION_LABEL[row['section']], 'changes': summary.get('changes') or [],
        'reason': row['reason'], 'hasProof': bool(row['proof_file_id']), 'requestedBy': by['name'] if by else None,
        'byHr': bool(summary.get('byHr')),
    }


def register() -> None:
    """Wire the decision handler and the inbox summary (called from PeopleConfig.ready)."""
    from hajiri.workflow.services import register_handler, register_summariser
    register_handler(PROFILE_CHANGE, decided)
    register_summariser(PROFILE_CHANGE, summarise)
