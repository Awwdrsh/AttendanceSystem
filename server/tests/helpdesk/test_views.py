"""Pages and HTMX actions of the help desk (HelpdeskPage, MyTicketsPage, TicketThread) through the test client."""
import pytest
from django.test import Client

from tests.support.sql import one
from hajiri.helpdesk import services as hd
from tests.web.factories import htmx, hx_triggers, set_features_off, sign_in, toasts

from .world import build, raise_tickets, upload


@pytest.fixture
def w(db):
    return build()


def client_for(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


def test_my_help_page_lists_my_tickets_and_empty_state(w):
    c = client_for(w.sita)
    r = c.get('/me/help')
    assert r.status_code == 200
    assert b'No questions yet' in r.content and b'Raise a grievance' in r.content
    raise_tickets(w)
    r = c.get('/me/help')
    assert b'SSF missing on payslip' in r.content and b'Balance looks wrong' not in r.content
    r = c.get('/me/help', **htmx('hd-mine'))
    assert b'id="hd-mine"' in r.content and b'<html' not in r.content


def test_ask_hr_form_and_create(w):
    c = client_for(w.sita)
    r = c.get('/_/helpdesk/tickets/new', **htmx('hj-modal-body'))
    assert r.status_code == 200 and b'Payroll &amp; tax' in r.content and b'Send to HR' in r.content
    fid = upload(w.sita)
    r = c.post('/_/helpdesk/tickets/new', {'category_id': w.cat('Payroll'), 'subject': 'SSF missing', 'body': 'No SSF line.',
                                           'priority': 'urgent', 'file_ids': [fid, '']}, **htmx())
    assert r.status_code == 204
    tid = one('SELECT id FROM helpdesk_ticket WHERE subject = %s', ['SSF missing'])['id']
    assert r.headers['HX-Redirect'] == f'/me/help?ticket={tid}'
    assert toasts(r)[0]['message'] == 'Ticket #1 raised. We will get back to you.'
    assert one('SELECT priority FROM helpdesk_ticket WHERE id = %s', [tid])['priority'] == 'urgent'
    # The page opens the new ticket in the drawer.
    assert f'/_/helpdesk/tickets/{tid}'.encode() in c.get(f'/me/help?ticket={tid}').content
    # Validation errors come back as a toast.
    r = c.post('/_/helpdesk/tickets/new', {'category_id': w.cat('Other'), 'subject': 'x', 'body': ''}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['tone'] == 'err'


def test_thread_reply_rate_reopen_close(w):
    raise_tickets(w)
    tid = w.ticket['id']
    hr, sita = client_for(w.hr), client_for(w.sita)
    r = hr.get(f'/_/helpdesk/tickets/{tid}', **htmx('hj-drawer-body'))
    assert r.status_code == 200 and b'Assigned to' in r.content and b'Internal note' in r.content
    r = hr.post(f'/_/helpdesk/tickets/{tid}/messages', {'body': 'Only for us', 'internal': 'on'}, **htmx('hd-thread'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Note added.'
    assert 'hj:refresh-hd-queue' in hx_triggers(r)
    r = hr.post(f'/_/helpdesk/tickets/{tid}/messages', {'body': 'Fixed it.'}, **htmx('hd-thread'))
    assert toasts(r)[0]['message'] == 'Message sent.'
    r = sita.get(f'/_/helpdesk/tickets/{tid}', **htmx('hj-drawer-body'))
    assert b'Fixed it.' in r.content and b'Only for us' not in r.content and b'Internal note' not in r.content
    # The requester's file downloads; others get 404.
    fid = hd.ticket_detail(w.sita, tid)['messages'][0]['file_ids'][0]
    d = sita.get(f'/_/helpdesk/tickets/{tid}/files/{fid}')
    assert d.status_code == 200 and d['Content-Type'].startswith('application/pdf')
    assert client_for(w.ram).get(f'/_/helpdesk/tickets/{tid}/files/{fid}', **htmx()).status_code == 404
    # Only the team changes a ticket.
    r = sita.post(f'/_/helpdesk/tickets/{tid}/update', {'status': 'resolved'}, **htmx('hd-thread'))
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'only the help desk team can change a ticket'
    r = hr.post(f'/_/helpdesk/tickets/{tid}/update', {'status': 'resolved'}, **htmx('hd-thread'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Status changed.'
    r = hr.post(f'/_/helpdesk/tickets/{tid}/update', {'assignee_user_id': w.it_.id}, **htmx('hd-thread'))
    assert toasts(r)[0]['message'] == 'Assigned.'
    r = sita.get(f'/_/helpdesk/tickets/{tid}', **htmx('hj-drawer-body'))
    assert b'How did we do?' in r.content and b'Reopen' in r.content
    r = sita.post(f'/_/helpdesk/tickets/{tid}/rate', {'rating': '4', 'comment': ''}, **htmx('hd-thread'))
    assert toasts(r)[0]['message'] == 'Thanks for the feedback.' and b'Rated 4 of 5' in r.content
    r = sita.post(f'/_/helpdesk/tickets/{tid}/reopen', **htmx('hd-thread'))
    assert toasts(r)[0]['message'] == 'Ticket reopened.'
    r = sita.post(f'/_/helpdesk/tickets/{tid}/close', **htmx('hd-thread'))
    assert toasts(r)[0]['message'] == 'Ticket closed.' and b'This ticket is closed.' in r.content
    r = sita.post(f'/_/helpdesk/tickets/{tid}/messages', {'body': 'more'}, **htmx('hd-thread'))
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'this ticket is closed; raise a new ticket instead'


def test_ticket_link_opens_page_with_drawer(w):
    raise_tickets(w)
    tid = w.ticket['id']
    assert client_for(w.sita).get(f'/_/helpdesk/tickets/{tid}')['Location'] == f'/me/help?ticket={tid}'
    assert client_for(w.hr).get(f'/_/helpdesk/tickets/{tid}')['Location'] == f'/helpdesk?ticket={tid}'
    assert client_for(w.ram).get(f'/_/helpdesk/tickets/{tid}', **htmx()).status_code == 404


def test_hr_queue_page_filters_and_stats(w):
    raise_tickets(w)
    c = client_for(w.hr)
    r = c.get('/helpdesk')
    assert r.status_code == 200
    for text in (b'Past due time', b'Average time to resolve', b'Satisfaction', b'SSF missing on payslip', b'Balance looks wrong'):
        assert text in r.content
    r = c.get('/helpdesk', {'search': 'ssf'}, **htmx('hd-queue'))
    assert b'SSF missing' in r.content and b'Balance looks wrong' not in r.content and b'<html' not in r.content
    r = c.get('/helpdesk', {'status': 'closed'}, **htmx('hd-queue'))
    assert b'Nothing here' in r.content
    assert b'hd-stats' in c.get('/helpdesk', **htmx('hd-stats')).content
    r = c.get('/helpdesk', {'status': 'bogus'}, **htmx('hd-queue'))
    assert r.status_code == 400


def test_hr_only_pages_and_actions(w):
    sita = client_for(w.sita)
    assert sita.get('/helpdesk', **htmx()).status_code == 403
    assert sita.post('/_/helpdesk/settings', {'sla_low': '1'}, **htmx()).status_code == 403
    assert sita.get('/_/helpdesk/categories/new', **htmx()).status_code == 403


def test_topics_and_response_times(w):
    c = client_for(w.hr)
    r = c.get('/helpdesk?tab=setup')
    assert r.status_code == 200 and b'Topics' in r.content and b'Response times' in r.content and b'HR team' in r.content
    assert b'Save topic' in c.get('/_/helpdesk/categories/new', **htmx('hj-modal-body')).content
    r = c.post('/_/helpdesk/categories/new', {'name': 'Travel', 'description': 'Trips', 'active': 'on', 'sort': '3'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Topic saved.' and 'hj:refresh-hd-topics' in hx_triggers(r)
    cid = one("SELECT id FROM helpdesk_category WHERE name = 'Travel'")['id']
    assert b'Edit topic' in c.get(f'/_/helpdesk/categories/{cid}', **htmx('hj-modal-body')).content
    r = c.post(f'/_/helpdesk/categories/{cid}', {'name': 'Travel', 'owner_user_id': w.it_.id, 'sort': '3'}, **htmx())
    assert r.status_code == 204
    row = one('SELECT active, owner_user_id FROM helpdesk_category WHERE id = %s', [cid])
    assert row['active'] is False and str(row['owner_user_id']) == w.it_.id
    r = c.post('/_/helpdesk/categories/new', {'name': 'Travel', 'active': 'on'}, **htmx())
    assert r.status_code == 409 and toasts(r)[0]['message'] == 'a category with this name already exists'
    r = c.post('/_/helpdesk/settings', {'sla_low': '96', 'sla_normal': '48', 'sla_high': '12', 'sla_urgent': '4',
                                        'auto_close_days': '5', 'reopen_days': '3'}, **htmx())
    assert r.status_code == 204 and toasts(r)[0]['message'] == 'Settings saved.'
    assert hd.get_settings(w.sita)['policy']['slaHours']['urgent'] == 4
    r = c.post('/_/helpdesk/settings', {'sla_low': '0', 'sla_normal': '48', 'sla_high': '12', 'sla_urgent': '4',
                                        'auto_close_days': '5', 'reopen_days': '3'}, **htmx())
    assert r.status_code == 400


def test_feature_off_hides_the_help_desk(w):
    set_features_off(w.co, 'helpdesk')
    c = client_for(w.sita)
    assert c.get('/me/help')['Location'] == '/'
    r = c.post('/_/helpdesk/tickets/new', {}, **htmx())
    assert r.status_code == 404
    assert client_for(w.hr).get('/helpdesk')['Location'] == '/'


def test_nav_entries(w):
    from hajiri.web.nav import build as build_nav
    nav = build_nav(w.hr, '/helpdesk')
    assert 'Help desk' in str(nav)


def test_open_ticket_param_that_is_not_an_id_is_ignored(w):
    # A hand-edited ?ticket= used to fail the page with a 500 (no URL for that id); it now just opens no drawer.
    r = client_for(w.sita).get('/me/help?ticket=garbage')
    assert r.status_code == 200 and b'hj-drawer-body' in r.content and b'/_/helpdesk/tickets/garbage' not in r.content
    assert client_for(w.hr).get('/helpdesk?ticket=garbage').status_code == 200


def test_category_owner_works_a_ticket_from_the_link(w):
    hd.update_category(w.hr, w.cat('IT'), {'name': 'IT & equipment', 'ownerUserId': w.it_.id})
    tid = hd.create_ticket(w.sita, {'categoryId': w.cat('IT'), 'subject': 'Laptop fan noisy', 'body': 'Very loud'})['id']
    it = client_for(w.it_)
    # Owners are not HR, so the link lands on their own help page with the drawer open.
    assert it.get(f'/_/helpdesk/tickets/{tid}')['Location'] == f'/me/help?ticket={tid}'
    r = it.get(f'/_/helpdesk/tickets/{tid}', **htmx('hj-drawer-body'))
    assert r.status_code == 200 and b'name="status"' in r.content and b'name="assignee_user_id"' not in r.content
    assert b'Assigned to' in r.content
    r = it.post(f'/_/helpdesk/tickets/{tid}/update', {'status': 'waiting'}, **htmx('hd-thread'))
    assert r.status_code == 200 and toasts(r)[0]['message'] == 'Status changed.'
    r = it.post(f'/_/helpdesk/tickets/{tid}/update', {'assignee_user_id': ''}, **htmx('hd-thread'))
    assert r.status_code == 403 and toasts(r)[0]['message'] == 'only HR can reassign or move a ticket'
    # The confidential HR-only queue stays HR-only.
    assert it.get('/helpdesk', **htmx()).status_code == 403
