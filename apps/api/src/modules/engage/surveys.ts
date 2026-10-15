import { randomInt } from 'node:crypto';
import { z } from 'zod';
import { bsLabel } from '../../bsdate.js';
import type { FieldCipher } from '../../crypto.js';
import type { Db, Queryable } from '../../db/pool.js';
import { BadRequestError } from '../../errors.js';
import { notify } from '../notifications/service.js';
import { audienceMembers, inTx, type AudienceKind } from './service.js';

export type QuestionKind = 'rating' | 'enps' | 'single' | 'multi' | 'text';

export const questionSchema = z.object({
  kind: z.enum(['rating', 'enps', 'single', 'multi', 'text']),
  prompt: z.string().trim().min(1).max(300),
  options: z.array(z.string().trim().min(1).max(120)).max(12).default([]),
  required: z.boolean().default(true),
}).superRefine((q, ctx) => {
  if ((q.kind === 'single' || q.kind === 'multi') && q.options.length < 2) ctx.addIssue({ code: 'custom', message: 'choice questions need at least two options', path: ['options'] });
  if ((q.kind === 'single' || q.kind === 'multi') && new Set(q.options.map((o) => o.toLowerCase())).size !== q.options.length) ctx.addIssue({ code: 'custom', message: 'options must be different', path: ['options'] });
});

export interface SurveyRow {
  id: string; company_id: string; kind: 'survey' | 'poll'; title: string; description: string | null; anonymous: boolean; min_group: number;
  audience_kind: AudienceKind; audience_id: string | null; status: 'draft' | 'published' | 'closed'; opens_on: string; closes_on: string;
  recurrence: 'none' | 'weekly' | 'monthly'; series_id: string | null; created_by: string | null; created_at: Date; published_at: Date | null;
  opened_notified_at: Date | null; reminded_at: Date | null; closed_at: Date | null;
}
export interface QuestionRow { id: string; survey_id: string; position: number; kind: QuestionKind; prompt: string; options: string[]; required: boolean }

/** Keyed hash of (survey, user): stops double answers without storing who answered an anonymous survey. */
export const respondentKey = (cipher: FieldCipher, surveyId: string, userId: string) => cipher.blindIndex(`survey:${surveyId}:${userId}`);

export async function questionsOf(db: Queryable, surveyId: string): Promise<QuestionRow[]> {
  const r = await db.query<QuestionRow>('SELECT * FROM survey_question WHERE survey_id = $1 ORDER BY position', [surveyId]);
  return r.rows;
}

export const isOpen = (s: SurveyRow, today: string) => s.status === 'published' && s.opens_on <= today && s.closes_on >= today;

export const answerSchema = z.object({
  questionId: z.uuid(),
  value: z.number().int().optional(),
  choices: z.array(z.string().max(120)).max(12).optional(),
  text: z.string().max(2000).optional(),
});
export type Answer = z.infer<typeof answerSchema>;

/** Validate a full set of answers against the questions; returns the rows to store. */
export function checkAnswers(questions: QuestionRow[], answers: Answer[]): Array<{ questionId: string; value: number | null; choices: string[] | null; text: string | null }> {
  const byId = new Map(answers.map((a) => [a.questionId, a]));
  if (byId.size !== answers.length) throw new BadRequestError('Each question can be answered once.');
  for (const a of answers) if (!questions.some((q) => q.id === a.questionId)) throw new BadRequestError('An answer refers to a question that is not in this survey.');
  const out: Array<{ questionId: string; value: number | null; choices: string[] | null; text: string | null }> = [];
  for (const q of questions) {
    const a = byId.get(q.id);
    const text = a?.text?.trim() ?? '';
    const empty = !a || (a.value === undefined && !(a.choices?.length) && !text);
    if (empty) {
      if (q.required) throw new BadRequestError(`Please answer: ${q.prompt}`);
      continue;
    }
    switch (q.kind) {
      case 'rating':
        if (a.value === undefined || a.value < 1 || a.value > 5) throw new BadRequestError(`Pick a rating from 1 to 5 for: ${q.prompt}`);
        out.push({ questionId: q.id, value: a.value, choices: null, text: null }); break;
      case 'enps':
        if (a.value === undefined || a.value < 0 || a.value > 10) throw new BadRequestError(`Pick a score from 0 to 10 for: ${q.prompt}`);
        out.push({ questionId: q.id, value: a.value, choices: null, text: null }); break;
      case 'single':
        if (a.choices?.length !== 1 || !q.options.includes(a.choices[0]!)) throw new BadRequestError(`Pick one option for: ${q.prompt}`);
        out.push({ questionId: q.id, value: null, choices: a.choices, text: null }); break;
      case 'multi': {
        const c = [...new Set(a.choices ?? [])];
        if (!c.length || c.some((x) => !q.options.includes(x))) throw new BadRequestError(`Pick from the listed options for: ${q.prompt}`);
        out.push({ questionId: q.id, value: null, choices: c, text: null }); break;
      }
      case 'text':
        if (!text) throw new BadRequestError(`Please answer: ${q.prompt}`);
        out.push({ questionId: q.id, value: null, choices: null, text }); break;
    }
  }
  return out;
}

// ---------------------------------------------------------------- results

export interface QuestionResult {
  questionId: string; kind: QuestionKind; prompt: string; options: string[]; answered: number;
  distribution?: Array<{ label: string; count: number }>;
  average?: number | null;
  enps?: { score: number; promoters: number; passives: number; detractors: number } | null;
  texts?: Array<{ text: string; name?: string }>;
  breakdown?: Array<{ group: string; responses: number; average: number | null }>;
}

export interface SurveyResults {
  responses: number;
  audience: number;
  hidden: boolean;          // anonymous and below the minimum group size
  minGroup: number;
  questions: QuestionResult[];
}

/** Fisher–Yates with a CSPRNG so text answers never come back in submission order. */
function shuffle<T>(xs: T[]): T[] {
  const a = [...xs];
  for (let i = a.length - 1; i > 0; i--) { const j = randomInt(i + 1); [a[i], a[j]] = [a[j]!, a[i]!]; }
  return a;
}

const avg = (xs: number[]) => (xs.length ? Math.round((xs.reduce((s, x) => s + x, 0) / xs.length) * 100) / 100 : null);

export function enpsOf(values: number[]): { score: number; promoters: number; passives: number; detractors: number } | null {
  if (!values.length) return null;
  const promoters = values.filter((v) => v >= 9).length;
  const detractors = values.filter((v) => v <= 6).length;
  return { score: Math.round(((promoters - detractors) / values.length) * 100), promoters, passives: values.length - promoters - detractors, detractors };
}

/**
 * Aggregated results. Anonymous surveys show nothing until `min_group` responses arrive; department breakdowns only
 * list groups that reach the minimum (smaller groups are pooled into "Other groups" when that pool is large enough);
 * text answers are shuffled and carry no names, dates or ids.
 */
export async function surveyResults(db: Queryable, s: SurveyRow, audienceSize: number): Promise<SurveyResults> {
  const questions = await questionsOf(db, s.id);
  const resp = await db.query<{ id: string; org_unit_id: string | null; unit_name: string | null; employee_name: string | null }>(
    `SELECT r.id, r.org_unit_id, o.name_en AS unit_name, e.name_en AS employee_name
       FROM survey_response r LEFT JOIN org_unit o ON o.id = r.org_unit_id LEFT JOIN employee e ON e.id = r.employee_id
      WHERE r.survey_id = $1`, [s.id]);
  const n = resp.rows.length;
  const hidden = s.anonymous && n < s.min_group;
  if (hidden) return { responses: n, audience: audienceSize, hidden, minGroup: s.min_group, questions: [] };
  const ans = await db.query<{ response_id: string; question_id: string; value_num: number | null; choices: string[] | null; text_value: string | null }>(
    `SELECT a.response_id, a.question_id, a.value_num, a.choices, a.text_value
       FROM survey_answer a JOIN survey_response r ON r.id = a.response_id WHERE r.survey_id = $1`, [s.id]);
  const respById = new Map(resp.rows.map((r) => [r.id, r]));
  const results: QuestionResult[] = questions.map((q) => {
    const rows = ans.rows.filter((a) => a.question_id === q.id);
    const base: QuestionResult = { questionId: q.id, kind: q.kind, prompt: q.prompt, options: q.options, answered: rows.length };
    if (q.kind === 'rating' || q.kind === 'enps') {
      const vals = rows.map((a) => a.value_num!).filter((v) => v !== null);
      const scale = q.kind === 'rating' ? [1, 2, 3, 4, 5] : [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10];
      base.distribution = scale.map((v) => ({ label: String(v), count: vals.filter((x) => x === v).length }));
      base.average = avg(vals);
      if (q.kind === 'enps') base.enps = enpsOf(vals);
      const groups = new Map<string, number[]>();
      for (const a of rows) {
        const g = respById.get(a.response_id)?.unit_name ?? 'No department';
        groups.set(g, [...(groups.get(g) ?? []), a.value_num!]);
      }
      const shown: Array<{ group: string; responses: number; average: number | null }> = [];
      const pooled: number[] = [];
      for (const [group, v] of groups) {
        if (v.length >= s.min_group) shown.push({ group, responses: v.length, average: q.kind === 'enps' ? enpsOf(v)!.score : avg(v) });
        else pooled.push(...v);
      }
      if (pooled.length >= s.min_group && shown.length) shown.push({ group: 'Other groups', responses: pooled.length, average: q.kind === 'enps' ? enpsOf(pooled)!.score : avg(pooled) });
      base.breakdown = shown.length > 1 ? shown.sort((a, b) => b.responses - a.responses) : [];
    } else if (q.kind === 'single' || q.kind === 'multi') {
      base.distribution = q.options.map((o) => ({ label: o, count: rows.filter((a) => a.choices?.includes(o)).length }));
    } else {
      const texts = rows.filter((a) => a.text_value).map((a) => ({ text: a.text_value!, ...(s.anonymous ? {} : { name: respById.get(a.response_id)?.employee_name ?? '' }) }));
      base.texts = s.anonymous ? shuffle(texts) : texts;
    }
    return base;
  });
  return { responses: n, audience: audienceSize, hidden: false, minGroup: s.min_group, questions: results };
}

// ---------------------------------------------------------------- daily job

const DAY_MS = 86_400_000;
const addAd = (ad: string, days: number) => new Date(Date.parse(`${ad}T00:00:00Z`) + days * DAY_MS).toISOString().slice(0, 10);
const daysBetween = (a: string, b: string) => Math.round((Date.parse(`${b}T00:00:00Z`) - Date.parse(`${a}T00:00:00Z`)) / DAY_MS);

export async function notifyOpened(db: Queryable, s: SurveyRow): Promise<number> {
  const members = await audienceMembers(db, s.company_id, s.audience_kind, s.audience_id, null);
  for (const m of members) {
    if (!m.user_id) continue;
    await notify(db, { companyId: s.company_id, userId: m.user_id, employeeId: m.employee_id, template: 'engage.survey_open',
      title: s.kind === 'poll' ? 'New poll' : 'New survey', body: `${s.title}${s.anonymous ? ' (anonymous)' : ''}. Open until ${bsLabel(s.closes_on)}.`, payload: { surveyId: s.id } });
  }
  await db.query('UPDATE survey SET opened_notified_at = now() WHERE id = $1', [s.id]);
  return members.length;
}

/**
 * Once a day: close surveys past their end date (and start the next round of a recurring pulse), announce surveys that
 * opened today, and remind people who have not answered a survey closing tomorrow.
 */
export async function runSurveyJobs(db: Db, cipher: FieldCipher, today: string): Promise<{ closed: number; started: number; opened: number; reminded: number }> {
  const out = { closed: 0, started: 0, opened: 0, reminded: 0 };
  const due = await db.query<SurveyRow>(`SELECT * FROM survey WHERE status = 'published' AND closes_on < $1::date`, [today]);
  for (const s of due.rows) {
    await inTx(db, async (c) => {
      const u = await c.query(`UPDATE survey SET status = 'closed', closed_at = now() WHERE id = $1 AND status = 'published'`, [s.id]);
      if (!u.rowCount) return;
      out.closed++;
      if (s.recurrence === 'none') return;
      const length = daysBetween(s.opens_on, s.closes_on);
      const step = s.recurrence === 'weekly' ? 7 : 30;
      // Next round starts on the following cycle date and lasts as long as the first; rounds missed entirely are skipped.
      let opens = addAd(s.opens_on, step);
      while (addAd(opens, length) < today) opens = addAd(opens, step);
      const next = await c.query<{ id: string }>(
        `INSERT INTO survey (company_id, kind, title, description, anonymous, min_group, audience_kind, audience_id, status, opens_on, closes_on,
                             recurrence, series_id, created_by, published_at)
         SELECT company_id, kind, title, description, anonymous, min_group, audience_kind, audience_id, 'published', $2, $3,
                recurrence, coalesce(series_id, id), created_by, now() FROM survey WHERE id = $1 RETURNING id`,
        [s.id, opens, addAd(opens, length)]);
      await c.query(
        `INSERT INTO survey_question (survey_id, position, kind, prompt, options, required)
         SELECT $2, position, kind, prompt, options, required FROM survey_question WHERE survey_id = $1`, [s.id, next.rows[0]!.id]);
      out.started++;
    });
  }
  const opening = await db.query<SurveyRow>(
    `SELECT * FROM survey WHERE status = 'published' AND opened_notified_at IS NULL AND opens_on <= $1::date AND closes_on >= $1::date`, [today]);
  for (const s of opening.rows) { await notifyOpened(db, s); out.opened++; }
  const closing = await db.query<SurveyRow>(
    `SELECT * FROM survey WHERE status = 'published' AND reminded_at IS NULL AND closes_on = $1::date + 1 AND opens_on < $1::date`, [today]);
  for (const s of closing.rows) {
    const members = await audienceMembers(db, s.company_id, s.audience_kind, s.audience_id, null);
    const answered = new Set((await db.query<{ respondent_key: string }>('SELECT respondent_key FROM survey_response WHERE survey_id = $1', [s.id])).rows.map((r) => r.respondent_key));
    for (const m of members) {
      if (!m.user_id || answered.has(respondentKey(cipher, s.id, m.user_id))) continue;
      await notify(db, { companyId: s.company_id, userId: m.user_id, employeeId: m.employee_id, template: 'engage.survey_reminder',
        title: 'Survey closes tomorrow', body: `You have not answered "${s.title}" yet. It takes a minute.`, payload: { surveyId: s.id } });
      out.reminded++;
    }
    await db.query('UPDATE survey SET reminded_at = now() WHERE id = $1', [s.id]);
  }
  return out;
}
