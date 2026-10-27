"""Announcements, kudos, surveys and suggestions (port of apps/api/src/db/demo-suite/engage.ts)."""
from __future__ import annotations

from hajiri.core.db import rows
from hajiri.engage import services as engage

from .context import DemoContext, already_seeded
from .kit import ApiError, add_days_iso, pick

KUDOS = [
    (2, [3], 'Thank you for staying late to finish the Nabil cut-over. The customer noticed!'),
    (4, [5, 6], 'Great teamwork on the KU workshop: 24 students onboarded without a hitch.'),
    (7, [1], 'Thanks for helping me understand the SSF changes on my payslip, so patient.'),
    (3, [8], 'Your write-up of the latency incident was the clearest I have read.'),
    (9, [10], 'Closed the Himalayan Bank deal while juggling three demos. Legend.'),
    (5, [2], 'You fixed the flaky console tests that bothered all of us for weeks. Thank you!'),
    (11, [12], 'Thanks for organising the team lunch and the farewell card.'),
]
TEXTS = ['A quieter room for calls.', 'Faster laptop refresh for engineers.', 'More knowledge sharing sessions.', 'Clearer goals for the quarter.', '',
         'Better coffee!', '']


def seed_engage(ctx: DemoContext) -> None:
    """Announcements, kudos with reactions, birthdays shown by choice, a pulse survey with enough answers to show results, a poll and suggestions."""
    kit, hr, today = ctx.kit, ctx.hr, ctx.today
    if already_seeded(ctx, 'engage', 'SELECT 1 FROM suggestion WHERE company_id = %s LIMIT 1', 'SELECT 1 FROM announcement WHERE company_id = %s LIMIT 1'):
        return
    n = len(ctx.employees)

    def ann(b: dict) -> str:
        return str(kit.call('POST /engage/announcements', engage.post_announcement, hr.user, b)['id'])
    a1 = ann({'title': 'Office closed for Dashain and Tihar', 'pinned': True, 'requiresAck': False, 'expiresOn': add_days_iso(today, 25),
              'body': 'The office is closed from Ghatasthapana to Bhai Tika as per the holiday calendar in Hajiri. On-call engineers have their roster under My attendance.'
                      '\n\nHappy Dashain to you and your families!'})
    ann({'title': 'New GPU racks are live in Bhaisepati',
         'body': 'Thanks to the platform and datacenter teams, 64 more GPUs went live this week. Customer quotas were raised today.'})
    ann({'title': 'Updated Employee Handbook 2083', 'requiresAck': False,
         'body': 'The handbook now covers work from home and the new travel allowance rates. Please read and acknowledge it under Profile, Documents & letters.'})

    values = kit.call('GET /engage/values', engage.list_values, hr.user)['values']
    ids: list[str] = []
    for i, (frm, to, message) in enumerate(KUDOS):
        giver = ctx.employees[frm % n]
        recipients = [r for r in (ctx.employees[t % n].id for t in to) if r != giver.id]
        if not recipients:
            continue
        r = kit.call('POST /engage/kudos', engage.give_kudos, ctx.as_(giver).user,
                     {'recipientIds': recipients, 'message': message, 'valueId': str(pick(values, i)['id']) if values else None})
        ids.append(str(r['id']))
    for i, kid in enumerate(ids):
        for k in range(3):
            e = ctx.employees[(i * 3 + k + 1) % n]
            try:
                kit.call('POST /engage/reactions', engage.react, ctx.as_(e).user,
                         {'targetType': 'kudos', 'targetId': kid, 'emoji': pick(['clap', 'love', 'celebrate', 'like'], i + k)})
            except ApiError:
                pass
    kit.call('POST /engage/comments', engage.add_comment, ctx.as_(ctx.employees[3]).user, {'targetType': 'announcement', 'targetId': a1, 'body': 'Happy Dashain everyone!'})

    # Half the team chose to show their birthday.
    for e in ctx.employees[::2]:
        kit.call('PUT /engage/me/prefs', engage.save_prefs, ctx.as_(e).user, {'showBirthday': True})

    # Pulse survey (anonymous) with answers from most of the team, and a quick poll.
    s = str(kit.call('POST /engage/surveys', engage.create_survey, hr.user, {
        'kind': 'survey', 'title': 'Monthly pulse: Ashwin',
        'description': 'Five questions, two minutes. Answers are anonymous; results show once five people have answered.', 'anonymous': True, 'minGroup': 5,
        'opensOn': add_days_iso(today, -6), 'closesOn': add_days_iso(today, 4), 'recurrence': 'monthly',
        'questions': [
            {'kind': 'enps', 'prompt': 'How likely are you to recommend CoreValley as a place to work?'},
            {'kind': 'rating', 'prompt': 'I have the tools I need to do my job well.'},
            {'kind': 'rating', 'prompt': 'My manager gives me useful feedback.'},
            {'kind': 'single', 'prompt': 'How is your workload this month?', 'options': ['Too light', 'About right', 'Too heavy']},
            {'kind': 'text', 'prompt': 'What one thing should we change?', 'required': False},
        ],
    })['id'])
    kit.call(f'POST /engage/surveys/{s}/publish', engage.publish_survey, hr.user, s)
    qs = rows('SELECT id, kind FROM survey_question WHERE survey_id = %s ORDER BY position', [s])
    for i, e in enumerate(ctx.employees[:min(n - 2, 12)]):
        answers = []
        for q in qs:
            qid = str(q['id'])
            if q['kind'] == 'enps':
                answers.append({'questionId': qid, 'value': pick([9, 10, 8, 7, 9, 6, 10, 9, 3, 8], i)})
            elif q['kind'] == 'rating':
                answers.append({'questionId': qid, 'value': pick([4, 5, 3, 4, 4, 2, 5], i + len(qid))})
            elif q['kind'] == 'single':
                answers.append({'questionId': qid, 'choices': [pick(['About right', 'About right', 'Too heavy', 'About right', 'Too light'], i)]})
            elif pick(TEXTS, i):
                answers.append({'questionId': qid, 'text': pick(TEXTS, i)})
        kit.call(f'POST /engage/surveys/{s}/respond', engage.respond, ctx.as_(e).user, s, {'answers': answers})
    poll = str(kit.call('POST /engage/surveys', engage.create_survey, hr.user, {
        'kind': 'poll', 'title': 'Team outing after Tihar', 'anonymous': False, 'minGroup': 3, 'opensOn': today, 'closesOn': add_days_iso(today, 7),
        'questions': [{'kind': 'single', 'prompt': 'Where should we go for the team outing?', 'options': ['Nagarkot hike', 'Bhaktapur heritage walk', 'Futsal and dinner']}],
    })['id'])
    kit.call(f'POST /engage/surveys/{poll}/publish', engage.publish_survey, hr.user, poll)
    pq = str(rows('SELECT id FROM survey_question WHERE survey_id = %s', [poll])[0]['id'])
    for i, e in enumerate(ctx.employees[:7]):
        kit.call(f'POST /engage/surveys/{poll}/respond', engage.respond, ctx.as_(e).user, poll,
                 {'answers': [{'questionId': pq, 'choices': [pick(['Nagarkot hike', 'Futsal and dinner', 'Nagarkot hike', 'Bhaktapur heritage walk'], i)]}]})

    kit.call('POST /engage/suggestions', engage.suggest, ctx.as_(ctx.employees[5]).user,
             {'category': 'workplace', 'body': 'Could we get a second water dispenser on the third floor? The queue at lunch is long.'})
    kit.call('POST /engage/suggestions', engage.suggest, ctx.as_(ctx.employees[8]).user,
             {'category': 'process', 'body': 'Please share the agenda before all-hands so remote people can prepare questions.'})
