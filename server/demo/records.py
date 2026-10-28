"""Documents, assets, onboarding and exits, help desk (port of apps/api/src/db/demo-suite/records.ts)."""
from __future__ import annotations

import math
import re

from django.db.models.functions import Lower, Now

from hajiri.db.models import (
    Asset, AssetType, CompanyPolicy, Employee, EmployeeDocument, EmployeeExit, HelpdeskTicket, LifecycleCase,
)
from hajiri.documents import services as documents
from hajiri.helpdesk import services as helpdesk
from hajiri.lifecycle import api as lifecycle

from .context import DemoContext, already_seeded
from .kit import add_days_iso, base36, pick


def seed_documents(ctx: DemoContext) -> None:
    """Documents on file (verified, waiting, expiring), letters issued and requested, and a handbook to acknowledge."""
    kit, hr = ctx.kit, ctx.hr
    if already_seeded(ctx, 'documents', CompanyPolicy.objects.all(), EmployeeDocument.objects.all()):
        return
    cats = kit.call('GET /documents/categories', documents.list_categories, hr.user)

    def cat(code: str) -> str:
        return str(next(c for c in cats if c['code'] == code)['id'])
    tpls = kit.call('GET /documents/templates', documents.list_templates, hr.user)

    def tpl(code: str) -> str:
        return str(next(t for t in tpls if t['code'] == code)['id'])

    def add(who, employee_id: str, body: dict) -> None:
        kit.call(f'POST /documents/employee/{employee_id}', documents.add_document, who.user, employee_id, body)

    for i, e in enumerate(ctx.employees):
        contract = kit.upload_pdf(hr, 'employee_document', f'contract-{e.code}.pdf', f'Employment contract - {e.name}',
                                  [ctx.company_name, f"Position: {e.designation or 'Staff'}", f"Joining date (AD): {e.join_date or ''}", 'Signed by both parties.'])
        add(hr, e.id, {'categoryId': cat('contract'), 'title': 'Employment contract', 'fileId': contract})
        cz = kit.upload_pdf(hr, 'employee_document', f'citizenship-{e.code}.pdf', f'Citizenship certificate - {e.name}', ['Copy kept on file by HR.'])
        add(hr, e.id, {'categoryId': cat('citizenship'), 'title': 'Citizenship certificate (front and back)', 'fileId': cz})
        if i % 2 == 0:
            ac = kit.upload_pdf(hr, 'employee_document', f'degree-{e.code}.pdf', f'Academic transcript - {e.name}', ['Bachelor degree transcript.'])
            add(hr, e.id, {'categoryId': cat('academic'), 'title': 'Bachelor transcript', 'fileId': ac})
        if i in (1, 5):
            pp = kit.upload_pdf(hr, 'employee_document', f'passport-{e.code}.pdf', f'Passport - {e.name}', ['Bio page.'])
            add(hr, e.id, {'categoryId': cat('passport'), 'title': 'Passport bio page', 'fileId': pp,
                           'expiryDate': add_days_iso(ctx.today, 45 if i == 1 else 12)})
    # HR uploads count as verified; a few employees uploaded their own and wait for HR.
    (EmployeeDocument.objects.for_company(ctx.company_id).filter(status=EmployeeDocument.Status.PENDING)
     .update(status=EmployeeDocument.Status.VERIFIED, verified_at=Now()))
    for e in ctx.employees[2:5]:
        me = ctx.as_(e)
        f = kit.upload_pdf(me, 'employee_document', f'pan-{e.code}.pdf', f'PAN card - {e.name}', ['Permanent Account Number card.'])
        add(me, e.id, {'categoryId': cat('pan_card'), 'title': 'PAN card', 'fileId': f})

    # Letters: appointment letters for the latest joiners, a salary certificate, and an experience letter request waiting for HR.
    for e in sorted(ctx.employees, key=lambda x: x.join_date or '', reverse=True)[:3]:
        kit.call('POST /documents/letters', documents.issue_letter, hr.user, {'templateId': tpl('appointment'), 'employeeId': e.id, 'allowMissing': True})
    s = ctx.employees[4]
    kit.call('POST /documents/letters', documents.issue_letter, hr.user,
             {'templateId': tpl('salary_certificate'), 'employeeId': s.id, 'purpose': 'Home loan application', 'addressedTo': 'Nabil Bank Limited',
              'allowMissing': True})
    asker = ctx.employees[6]
    kit.call('POST /documents/letter-requests', documents.request_letter, ctx.as_(asker).user,
             {'templateId': tpl('salary_certificate'), 'purpose': 'Visa application for a family trip', 'addressedTo': 'Embassy of Japan, Kathmandu'})
    asker2 = ctx.employees[9]
    kit.call('POST /documents/letter-requests', documents.request_letter, ctx.as_(asker2).user,
             {'templateId': tpl('to_whom_it_may_concern'), 'purpose': 'Bank account opening'})

    # Company policies.
    handbook = kit.upload_pdf(hr, 'company_document', 'employee-handbook-2083.pdf', f'{ctx.company_name} Employee Handbook 2083',
                              ['1. Working hours: Sunday to Friday, 9:30 to 18:00.', '2. Leave: apply in Hajiri at least two days ahead.',
                               '3. Work from home: agree with your manager first.', '4. Code of conduct applies to everyone.'])
    hb = kit.call('POST /documents/policies', documents.publish_policy, hr.user,
                  {'title': 'Employee Handbook 2083', 'description': 'Working hours, leave, work from home and conduct. Please read and acknowledge.',
                   'fileId': handbook, 'requiresAck': True})
    it = kit.upload_pdf(hr, 'company_document', 'it-acceptable-use.pdf', 'IT acceptable use policy',
                        ['Use company laptops for work.', 'Lock your screen when away.', 'Report lost devices to IT within a day.'])
    kit.call('POST /documents/policies', documents.publish_policy, hr.user,
             {'title': 'IT acceptable use', 'description': 'How to use company laptops, accounts and data safely.', 'fileId': it, 'requiresAck': True})
    for e in ctx.employees[:math.ceil(len(ctx.employees) * 0.6)]:
        kit.call(f"POST /documents/policies/{hb['id']}/acknowledge", documents.acknowledge_policy, ctx.as_(e).user, hb['id'])


def seed_assets(ctx: DemoContext) -> None:
    """Laptops, phones and ID cards, most of them handed out."""
    kit, hr = ctx.kit, ctx.hr
    if already_seeded(ctx, 'assets', Asset.objects.filter(tag='LAP-OLD-7'), Asset.objects.all()):
        return
    lifecycle.ensure_defaults(hr.user)
    kit.call('GET /lifecycle/asset-types', lifecycle.asset_types, hr.user)  # creates the default types on first use

    def type_id(name: str) -> str:
        found = (AssetType.objects.for_company(ctx.company_id).alias(lname=Lower('name')).filter(lname=name.lower())
                 .values_list('id', flat=True).first())
        if found:
            return str(found)
        return str(kit.call('POST /lifecycle/asset-types', lifecycle.add_asset_type, hr.user, {'name': name})['id'])
    laptop, phone, card = type_id('Laptop'), type_id('Mobile phone'), type_id('ID card')
    models = ['MacBook Pro 14 M3', 'Dell Latitude 7440', 'ThinkPad X1 Carbon Gen 11', 'MacBook Air 13 M2']

    def create(body: dict) -> str:
        return str(kit.call('POST /lifecycle/assets', lifecycle.create_asset, hr.user, body))

    def assign(asset_id: str, body: dict) -> None:
        kit.call(f'POST /lifecycle/assets/{asset_id}/assign', lifecycle.assign_asset, hr.user, asset_id, body)
    for i, e in enumerate(ctx.employees):
        on = e.join_date if e.join_date and e.join_date < ctx.today else ctx.today
        lap = create({'tag': f'LAP-{101 + i}', 'name': pick(models, i), 'typeId': laptop, 'serial': f'SN{base36(734211 + i * 97).upper()}',
                      'condition': 'new' if i % 5 == 0 else 'good', 'purchaseDate': add_days_iso(e.join_date or ctx.today, -10),
                      'cost': pick(['185000.00', '160000.00', '210000.00', '145000.00'], i)})
        assign(lap, {'employeeId': e.id, 'assignedOn': on})
        c = create({'tag': f'ID-{1001 + i}', 'name': 'Staff ID card', 'typeId': card, 'condition': 'good'})
        assign(c, {'employeeId': e.id, 'assignedOn': on})
        if re.search(r'sales|growth|success|manager|head', e.designation or '', re.I):
            p = create({'tag': f'PH-{201 + i}', 'name': pick(['Samsung Galaxy A55', 'iPhone 13'], i), 'typeId': phone, 'condition': 'good', 'cost': '65000.00'})
            assign(p, {'employeeId': e.id})
    create({'tag': 'LAP-SPARE-1', 'name': 'Dell Latitude 7440 (spare)', 'typeId': laptop, 'condition': 'good', 'notes': 'Kept for new joiners.'})
    create({'tag': 'LAP-OLD-7', 'name': 'MacBook Air 2017', 'typeId': laptop, 'condition': 'poor', 'notes': 'Battery worn out; to be disposed.'})


def seed_lifecycle(ctx: DemoContext) -> None:
    """Onboarding for the latest joiners (part done), a probation review waiting for HR, and a resignation in clearance."""
    kit, hr = ctx.kit, ctx.hr
    if already_seeded(ctx, 'lifecycle', EmployeeExit.objects.all(), LifecycleCase.objects.all()):
        return
    lifecycle.ensure_defaults(hr.user)
    kit.call('GET /lifecycle/templates', lifecycle.templates, hr.user)
    newest = sorted(ctx.employees, key=lambda x: x.join_date or '', reverse=True)[:2]

    def done_tasks(case_id: str, n: int | None, note: str) -> None:
        detail = kit.call(f'GET /lifecycle/cases/{case_id}', lifecycle.case_detail, hr.user, case_id)
        tasks = detail['tasks']
        count = n if n is not None else math.ceil(len(tasks) * 0.6)
        for t in tasks[:count]:
            kit.call(f"PATCH /lifecycle/tasks/{t['id']}", lifecycle.update_task, hr.user, str(t['id']), {'status': 'done', 'note': note})
    for k, e in enumerate(newest):
        cid = str(kit.call('POST /lifecycle/cases', lifecycle.start_onboarding, hr.user,
                           {'employeeId': e.id, 'anchorDate': add_days_iso(ctx.today, 5 if k == 0 else -12)}))
        done_tasks(cid, 2 if k == 0 else None, 'Done')

    # Probation: the newest joiner's manager recommends confirmation; it waits for HR in Approvals.
    on_probation = newest[1] if len(newest) > 1 else newest[0]
    (Employee.objects.for_company(ctx.company_id).filter(pk=on_probation.id, probation_end__isnull=True)
     .update(probation_end=add_days_iso(ctx.today, 9)))
    mgr = ctx.manager_of(on_probation)
    kit.call('POST /lifecycle/probation/reviews', lifecycle.submit_review, mgr.user,
             {'employeeId': on_probation.id, 'rating': 4, 'recommendation': 'confirm',
              'comments': 'Settled in quickly, owns the dashboard work end to end and helps others. Recommend confirming on time.'})

    # A resignation accepted by HR, with the last working day in three weeks and clearance under way.
    leaver = next((e for e in ctx.employees if re.search(r'network|technician|analyst', e.designation or '', re.I)), None) or ctx.employees[-3]
    kit.call('POST /lifecycle/exits', lifecycle.create_exit, hr.user,
             {'employeeId': leaver.id, 'kind': 'resignation', 'lastWorkingDay': add_days_iso(ctx.today, 21),
              'reasonCategory': 'Higher studies', 'reason': "Joining a master's programme in Australia in February."})
    x = kit.call('GET /lifecycle/cases?kind=offboarding', lifecycle.cases, hr.user, 'offboarding')
    cs = next((c for c in x if str(c['employee_id']) == leaver.id), None)
    if cs:
        done_tasks(str(cs['id']), 2, 'Handover notes received.')


TICKETS = [
    {'who': 3, 'cat': r'payroll', 'subject': 'SSF deduction looks higher this month',
     'body': 'My SSF deduction in Bhadra is Rs 400 more than Shrawan. Was there a change in basic salary?',
     'reply': 'Your basic was revised from Bhadra, so the 11% SSF share went up too. The revision letter is in your documents.', 'status': 'resolved', 'rate': 5},
    {'who': 5, 'cat': r'leave|attendance', 'subject': 'Missed punch on Wednesday',
     'body': 'The device at the door was offline in the morning, so my clock-in is missing. I arrived at 9:20.',
     'reply': 'Please raise a missed punch correction from My attendance and I will approve it today.', 'status': 'waiting'},
    {'who': 7, 'cat': r'document', 'subject': 'Need an experience letter for my old role',
     'body': 'Could I get a letter for my time in the support team before the transfer?', 'status': 'in_progress', 'reply': 'Drafting it now.'},
    {'who': 8, 'cat': r'it', 'subject': 'VPN keeps disconnecting from home', 'body': 'Since yesterday the VPN drops every 10 minutes when I work from home.',
     'priority': 'high'},
    {'who': 10, 'cat': r'other', 'subject': 'Parking space for two-wheelers',
     'body': 'Is there any plan to add more two-wheeler parking near the office? The lane is full by 9:30.'},
]


def seed_helpdesk(ctx: DemoContext) -> None:
    """Help desk tickets in every state, with replies."""
    kit, hr = ctx.kit, ctx.hr
    if already_seeded(ctx, 'helpdesk', HelpdeskTicket.objects.filter(subject='Parking space for two-wheelers'), HelpdeskTicket.objects.all()):
        return
    cats = kit.call('GET /helpdesk/categories', helpdesk.list_categories, hr.user)

    def cat(pattern: str) -> str:
        return str((next((c for c in cats if re.search(pattern, c['name'], re.I)), None) or cats[0])['id'])
    for t in TICKETS:
        e = ctx.employees[t['who'] % len(ctx.employees)]
        me = ctx.as_(e)
        r = kit.call('POST /helpdesk/tickets', helpdesk.create_ticket, me.user,
                     {'categoryId': cat(t['cat']), 'subject': t['subject'], 'body': t['body'], 'priority': t.get('priority', 'normal')})
        tid = str(r['id'])
        if t.get('reply'):
            kit.call(f'POST /helpdesk/tickets/{tid}/messages', helpdesk.add_message, hr.user, tid, {'body': t['reply']})
        if t.get('status'):
            kit.call(f'PATCH /helpdesk/tickets/{tid}', helpdesk.update_ticket, hr.user, tid, {'status': t['status']})
        if t.get('rate'):
            kit.call(f'POST /helpdesk/tickets/{tid}/rate', helpdesk.rate_ticket, me.user, tid, {'rating': t['rate'], 'comment': 'Quick and clear, thank you.'})
