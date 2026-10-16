import { alreadySeeded, type DemoContext } from './context.js';
import { addDaysIso, pick } from './kit.js';

/** Announcements, kudos with reactions, birthdays shown by choice, a pulse survey with enough answers to show results, a poll and suggestions. */
export async function seedEngage(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId, today } = ctx;
  if (await alreadySeeded(ctx, 'engage', 'SELECT 1 FROM suggestion WHERE company_id = $1 LIMIT 1', 'SELECT 1 FROM announcement WHERE company_id = $1 LIMIT 1')) return;

  const ann = async (b: Record<string, unknown>) => (await kit.post<{ announcement: { id: string } }>(hr, '/engage/announcements', b)).announcement.id;
  const a1 = await ann({ title: 'Office closed for Dashain and Tihar', pinned: true, requiresAck: false, expiresOn: addDaysIso(today, 25),
    body: 'The office is closed from Ghatasthapana to Bhai Tika as per the holiday calendar in Hajiri. On-call engineers have their roster under My attendance.\n\nHappy Dashain to you and your families!' });
  await ann({ title: 'New GPU racks are live in Bhaisepati', body: 'Thanks to the platform and datacenter teams, 64 more GPUs went live this week. Customer quotas were raised today.' });
  await ann({ title: 'Updated Employee Handbook 2083', requiresAck: false, body: 'The handbook now covers work from home and the new travel allowance rates. Please read and acknowledge it under Profile, Documents & letters.' });

  const values = (await kit.get<{ values: Array<{ id: string; name: string }> }>(hr, '/engage/values')).values;
  const kudos: Array<[number, number[], string]> = [
    [2, [3], 'Thank you for staying late to finish the Nabil cut-over. The customer noticed!'],
    [4, [5, 6], 'Great teamwork on the KU workshop: 24 students onboarded without a hitch.'],
    [7, [1], 'Thanks for helping me understand the SSF changes on my payslip, so patient.'],
    [3, [8], 'Your write-up of the latency incident was the clearest I have read.'],
    [9, [10], 'Closed the Himalayan Bank deal while juggling three demos. Legend.'],
    [5, [2], 'You fixed the flaky console tests that bothered all of us for weeks. Thank you!'],
    [11, [12], 'Thanks for organising the team lunch and the farewell card.'],
  ];
  const ids: string[] = [];
  for (const [i, [from, to, message]] of kudos.entries()) {
    const giver = ctx.employees[from % ctx.employees.length]!;
    const recipients = to.map((t) => ctx.employees[t % ctx.employees.length]!.id).filter((id) => id !== giver.id);
    if (!recipients.length) continue;
    const r = await kit.post<{ kudos: { id: string } } & { id?: string }>(await ctx.as(giver), '/engage/kudos', { recipientIds: recipients, message, valueId: values.length ? pick(values, i).id : null });
    ids.push(r.kudos?.id ?? r.id!);
  }
  for (const [i, id] of ids.entries()) {
    for (let k = 0; k < 3; k++) {
      const e = ctx.employees[(i * 3 + k + 1) % ctx.employees.length]!;
      await kit.post(await ctx.as(e), '/engage/reactions', { targetType: 'kudos', targetId: id, emoji: pick(['clap', 'love', 'celebrate', 'like'], i + k) }).catch(() => undefined);
    }
  }
  await kit.post(await ctx.as(ctx.employees[3]!), '/engage/comments', { targetType: 'announcement', targetId: a1, body: 'Happy Dashain everyone!' });

  // Half the team chose to show their birthday.
  for (const e of ctx.employees.filter((_, i) => i % 2 === 0)) await kit.put(await ctx.as(e), '/engage/me/prefs', { showBirthday: true });

  // Pulse survey (anonymous) with answers from most of the team, and a quick poll.
  const s = await kit.post<{ survey: { id: string } }>(hr, '/engage/surveys', {
    kind: 'survey', title: 'Monthly pulse: Ashwin', description: 'Five questions, two minutes. Answers are anonymous; results show once five people have answered.', anonymous: true, minGroup: 5,
    opensOn: addDaysIso(today, -6), closesOn: addDaysIso(today, 4), recurrence: 'monthly',
    questions: [
      { kind: 'enps', prompt: 'How likely are you to recommend CoreValley as a place to work?' },
      { kind: 'rating', prompt: 'I have the tools I need to do my job well.' },
      { kind: 'rating', prompt: 'My manager gives me useful feedback.' },
      { kind: 'single', prompt: 'How is your workload this month?', options: ['Too light', 'About right', 'Too heavy'] },
      { kind: 'text', prompt: 'What one thing should we change?', required: false },
    ],
  });
  await kit.post(hr, `/engage/surveys/${s.survey.id}/publish`, {});
  const qs = (await db.query<{ id: string; kind: string }>('SELECT id, kind FROM survey_question WHERE survey_id = $1 ORDER BY position', [s.survey.id])).rows;
  const texts = ['A quieter room for calls.', 'Faster laptop refresh for engineers.', 'More knowledge sharing sessions.', 'Clearer goals for the quarter.', '', 'Better coffee!', ''];
  for (const [i, e] of ctx.employees.slice(0, Math.min(ctx.employees.length - 2, 12)).entries()) {
    const answers = qs.map((q) => {
      if (q.kind === 'enps') return { questionId: q.id, value: pick([9, 10, 8, 7, 9, 6, 10, 9, 3, 8], i) };
      if (q.kind === 'rating') return { questionId: q.id, value: pick([4, 5, 3, 4, 4, 2, 5], i + q.id.length) };
      if (q.kind === 'single') return { questionId: q.id, choices: [pick(['About right', 'About right', 'Too heavy', 'About right', 'Too light'], i)] };
      return pick(texts, i) ? { questionId: q.id, text: pick(texts, i) } : null;
    }).filter(Boolean);
    await kit.post(await ctx.as(e), `/engage/surveys/${s.survey.id}/respond`, { answers });
  }
  const poll = await kit.post<{ survey: { id: string } }>(hr, '/engage/surveys', {
    kind: 'poll', title: 'Team outing after Tihar', anonymous: false, minGroup: 3, opensOn: today, closesOn: addDaysIso(today, 7),
    questions: [{ kind: 'single', prompt: 'Where should we go for the team outing?', options: ['Nagarkot hike', 'Bhaktapur heritage walk', 'Futsal and dinner'] }],
  });
  await kit.post(hr, `/engage/surveys/${poll.survey.id}/publish`, {});
  const pq = (await db.query<{ id: string }>('SELECT id FROM survey_question WHERE survey_id = $1', [poll.survey.id])).rows[0]!.id;
  for (const [i, e] of ctx.employees.slice(0, 7).entries()) {
    await kit.post(await ctx.as(e), `/engage/surveys/${poll.survey.id}/respond`, { answers: [{ questionId: pq, choices: [pick(['Nagarkot hike', 'Futsal and dinner', 'Nagarkot hike', 'Bhaktapur heritage walk'], i)] }] });
  }

  await kit.post(await ctx.as(ctx.employees[5]!), '/engage/suggestions', { category: 'workplace', body: 'Could we get a second water dispenser on the third floor? The queue at lunch is long.' });
  await kit.post(await ctx.as(ctx.employees[8]!), '/engage/suggestions', { category: 'process', body: 'Please share the agenda before all-hands so remote people can prepare questions.' });
}
