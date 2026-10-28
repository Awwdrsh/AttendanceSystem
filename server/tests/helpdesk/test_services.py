"""Port of apps/api/test/helpdesk.test.ts at the service level (same steps, same assertions)."""
from datetime import timedelta

import pytest

from hajiri.core.clock import now
from tests.support.sql import execute, one, rows
from hajiri.core.errors import ValidationError
from hajiri.helpdesk import services as hd
from hajiri.worker.registry import daily_jobs

from .world import assign_and_resolve, build, internal_note_and_reply, raise_grievance, raise_tickets, status, upload


@pytest.fixture
def w(db):
    return build()


def test_seeds_categories_without_grievance_topic_owners_hidden_from_employees(w):
    assert len(w.cats) == 6
    assert not [c for c in w.cats if c['name'] == 'Grievance']
    assert next(c for c in w.cats if c['name'].startswith('Confidential'))['confidential'] is True
    assert w.cats[0]['owner_user_id'] is None
    # Seeding happens once per company.
    assert hd.ensure_helpdesk_defaults(w.co.id) is False


def test_employee_raises_ticket_with_attachment_sequential_numbers_and_sla(w):
    file_id = upload(w.sita)
    r = hd.create_ticket(w.sita, {'categoryId': w.cat('Payroll'), 'subject': 'SSF missing on payslip',
                                  'body': 'My Bhadra payslip has no SSF line.', 'priority': 'high', 'fileIds': [file_id]})
    assert r['number'] == 1
    assert (r['slaDueAt'] - now()).total_seconds() > 23 * 3600
    r2 = hd.create_ticket(w.ram, {'categoryId': w.cat('Leave'), 'subject': 'Balance looks wrong', 'body': 'Please check'})
    assert r2['number'] == 2
    # Someone else's upload cannot be attached.
    other = upload(w.ram)
    assert status(hd.create_ticket, w.sita, {'categoryId': w.cat('Other'), 'subject': 'Steal', 'body': 'x', 'fileIds': [other]}) == 403
    assert one("SELECT count(*)::int AS n FROM helpdesk_ticket WHERE subject = 'Steal'")['n'] == 0
    n = one("""SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = %s AND template = 'helpdesk.ticket.new' AND channel = 'in_app'""",
            [w.hr.id])['n']
    assert n >= 2
    assert one("SELECT attached_to FROM stored_file WHERE id = %s", [file_id])['attached_to'].startswith('helpdesk_message:')


def test_only_requester_and_team_see_a_ticket(w):
    raise_tickets(w)
    tid = w.ticket['id']
    assert status(hd.ticket_detail, w.sita, tid) == 200
    assert status(hd.ticket_detail, w.ram, tid) == 404
    assert status(hd.ticket_detail, w.other_hr, tid) == 404
    assert hd.queue(w.ram, {}) == []
    assert len(hd.queue(w.hr, {})) == 2
    assert len(hd.my_tickets(w.sita)) == 1
    assert 'owner_user_id' not in hd.my_tickets(w.sita)[0]


def test_internal_notes_and_files_stay_inside_team_public_reply_is_first_response(w):
    raise_tickets(w)
    tid = w.ticket['id']
    note_file = upload(w.hr)
    assert status(hd.add_message, w.hr, tid, {'body': 'Check the SSF registration date.', 'internal': True, 'fileIds': [note_file]}, ok=201) == 201
    assert status(hd.add_message, w.sita, tid, {'body': 'sneaky', 'internal': True}) == 403
    as_sita = hd.ticket_detail(w.sita, tid)
    assert len(as_sita['messages']) == 1
    assert status(hd.ticket_file, w.sita, tid, note_file) == 404
    own = as_sita['messages'][0]['file_ids'][0]
    f = hd.ticket_file(w.sita, tid, own)
    assert f['mime'] == 'application/pdf' and f['data'].startswith(b'%PDF')
    assert status(hd.ticket_file, w.ram, tid, own) == 404
    hd.add_message(w.hr, tid, {'body': 'Fixed in the supplementary run.'})
    t = hd.ticket_detail(w.hr, tid)['ticket']
    assert t['status'] == 'in_progress'
    assert t['first_response_at']
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = %s AND template = 'helpdesk.ticket.reply'",
               [w.sita.id])['n'] >= 1


def test_only_hr_reassigns_then_assignee_works_the_ticket(w):
    raise_tickets(w)
    internal_note_and_reply(w)
    tid = w.ticket['id']
    assert status(hd.update_ticket, w.sita, tid, {'status': 'resolved'}) == 403
    assert status(hd.update_ticket, w.hr, tid, {'assigneeUserId': w.sita.id}) == 400
    assert status(hd.update_ticket, w.hr, tid, {'assigneeUserId': w.it_.id}) == 200
    assert [x['id'] for x in hd.queue(w.it_, {'assignee': 'me'})] == [tid]
    assert status(hd.update_ticket, w.it_, tid, {'assigneeUserId': w.ram.id}) == 403
    assert status(hd.update_ticket, w.it_, tid, {'status': 'resolved'}) == 200
    assert 'update' in [r['action'] for r in rows("SELECT action FROM audit_log WHERE row_id = %s", [tid])]
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = %s AND template = 'helpdesk.ticket.assigned'", [w.it_.id])['n'] >= 1
    assert one("SELECT count(*)::int AS n FROM notification_outbox WHERE user_id = %s AND template = 'helpdesk.ticket.resolved'", [w.sita.id])['n'] >= 1


def test_requester_rates_once_can_reopen_within_window_and_close(w):
    raise_tickets(w)
    internal_note_and_reply(w)
    assign_and_resolve(w)
    tid = w.ticket['id']
    assert status(hd.rate_ticket, w.hr, tid, {'rating': 5}) == 403
    assert status(hd.rate_ticket, w.sita, tid, {'rating': 2, 'comment': 'Slow'}) == 200
    assert status(hd.rate_ticket, w.sita, tid, {'rating': 5}) == 409
    # A low rating tells the assignee and HR.
    low = rows("SELECT user_id FROM notification_outbox WHERE template = 'helpdesk.ticket.low_rating' AND channel = 'in_app'")
    assert {str(r['user_id']) for r in low} >= {w.it_.id, w.hr.id}
    assert hd.ticket_detail(w.sita, tid)['canReopen'] is True
    assert status(hd.reopen_ticket, w.sita, tid) == 200
    t = hd.ticket_detail(w.sita, tid)['ticket']
    assert t['status'] == 'in_progress'
    assert t['rating'] is None
    assert status(hd.close_ticket, w.sita, tid) == 200
    assert status(hd.add_message, w.sita, tid, {'body': 'one more thing'}) == 409
    assert status(hd.close_ticket, w.sita, tid) == 409


def test_reopen_window_and_messages_after_resolution(w):
    raise_tickets(w)
    tid = w.ticket['id']
    assert status(hd.reopen_ticket, w.sita, tid) == 409          # still open
    hd.update_ticket(w.hr, tid, {'status': 'resolved'})
    execute("UPDATE helpdesk_ticket SET resolved_at = now() - interval '8 days' WHERE id = %s", [tid])
    assert hd.ticket_detail(w.sita, tid)['canReopen'] is False
    with pytest.raises(Exception) as e:
        hd.reopen_ticket(w.sita, tid)
    assert e.value.message == 'tickets can be reopened within 7 days of being resolved'
    with pytest.raises(Exception) as e:
        hd.add_message(w.sita, tid, {'body': 'hello again'})
    assert e.value.message == 'this ticket was resolved too long ago; raise a new ticket instead'
    # HR may still reopen it.
    assert status(hd.reopen_ticket, w.hr, tid) == 200


def test_grievances_never_visible_to_managers_or_non_hr_owners_only_hr_assigned(w):
    raise_tickets(w)
    raise_grievance(w)
    g = w.grievance
    assert status(hd.update_ticket, w.hr, g, {'assigneeUserId': w.ram.id}) == 400
    assert status(hd.update_category, w.hr, w.cat('Confidential'),
                  {'name': 'Confidential HR matter', 'confidential': True, 'ownerUserId': w.ram.id}) == 400
    assert status(hd.ticket_detail, w.ram, g) == 404
    n = rows("SELECT body FROM notification_outbox WHERE template = 'helpdesk.ticket.new' AND payload->>'ticketId' = %s", [g])
    assert n and all('manager' not in x['body'] for x in n)


def test_non_hr_category_owner_sees_tickets_in_their_category_only(w):
    raise_tickets(w)
    raise_grievance(w)
    assert status(hd.update_category, w.hr, w.cat('IT'), {'name': 'IT & equipment', 'ownerUserId': w.it_.id}) == 200
    r = hd.create_ticket(w.sita, {'categoryId': w.cat('IT'), 'subject': 'Laptop fan noisy', 'body': 'Very loud'})
    ids = [x['id'] for x in hd.queue(w.it_, {'status': 'active'})]
    assert r['id'] in ids
    assert w.grievance not in ids
    assert hd.ticket_detail(w.it_, r['id'])['role'] == 'agent'
    # The owner is assigned on creation and told about it.
    assert one("SELECT assignee_user_id FROM helpdesk_ticket WHERE id = %s", [r['id']])['assignee_user_id'] is not None


def test_daily_job_escalates_overdue_once_and_closes_old_resolved(w):
    raise_tickets(w)
    raise_grievance(w)
    execute("UPDATE helpdesk_ticket SET sla_due_at = now() - interval '1 hour' WHERE id = %s", [w.grievance])
    a = hd.helpdesk_daily()
    b = hd.helpdesk_daily()
    assert a['ticketsBreached'] >= 1
    assert b['ticketsBreached'] == 0
    assert w.grievance in [x['id'] for x in hd.queue(w.hr, {'breached': '1'})]
    hd.update_ticket(w.hr, w.grievance, {'status': 'resolved'})
    c = hd.helpdesk_daily(now() + timedelta(days=8))
    assert c['ticketsClosed'] >= 1
    s = hd.stats(w.hr)
    assert s['stats']['breached_90d'] >= 1
    assert status(hd.stats, w.sita) == 403
    breach = rows("SELECT body FROM notification_outbox WHERE template = 'helpdesk.sla_breached' AND payload->>'ticketId' = %s", [w.grievance])
    assert breach and all(x['body'].startswith('A confidential ticket') for x in breach)
    assert 'helpdesk' in [j.name for j in daily_jobs()]


def test_validates_input_and_settings(w):
    assert status(hd.create_ticket, w.sita, {'categoryId': w.cat('Other'), 'subject': 'x', 'body': ''}) == 400
    with pytest.raises(ValidationError):
        hd.create_ticket(w.sita, {'categoryId': w.cat('Other'), 'subject': 'x', 'body': ''})
    assert status(hd.put_settings, w.sita, {}) == 403
    s = hd.put_settings(w.hr, {'slaHours': {'low': 96, 'normal': 48, 'high': 12, 'urgent': 4}, 'autoCloseDays': 5, 'reopenDays': 3})
    assert s['policy']['reopenDays'] == 3
    assert hd.get_settings(w.sita)['policy']['slaHours']['urgent'] == 4
    assert status(hd.create_category, w.hr, {'name': 'Other'}) == 409
    assert status(hd.create_category, w.sita, {'name': 'Mine'}) == 403


def test_queue_filters_and_search(w):
    raise_tickets(w)
    assert [t['number'] for t in hd.queue(w.hr, {'search': '#2'})] == [2]
    assert [t['subject'] for t in hd.queue(w.hr, {'search': 'ssf'})] == ['SSF missing on payslip']
    assert hd.queue(w.hr, {'search': '100%'}) == []
    assert len(hd.queue(w.hr, {'assignee': 'none'})) == 2
    assert [t['priority'] for t in hd.queue(w.hr, {})] == ['high', 'normal']
    assert status(hd.queue, w.hr, {'status': 'nope'}) == 400
