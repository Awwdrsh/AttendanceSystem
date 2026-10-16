import { del, get, patch, post, put, type Query } from './client';

export type Reaction = 'like' | 'love' | 'clap' | 'celebrate' | 'laugh';
export type TargetType = 'kudos' | 'announcement' | 'celebration';
export type AudienceKind = 'all' | 'branch' | 'department' | 'reports';

interface Social { reactions: Partial<Record<Reaction, number>>; mine: Reaction[]; comments: number }

export interface KudosItem extends Social {
  type: 'kudos'; id: string; createdAt: string; message: string;
  giver: { employeeId: string; name: string; designation: string | null };
  recipients: Array<{ employeeId: string; name: string; designation: string | null }>;
  value: { id: string; name: string; emoji: string } | null; canDelete: boolean;
}
export interface AnnouncementItem extends Social {
  type: 'announcement'; id: string; createdAt: string; title: string; body: string; authorName: string; audienceKind: AudienceKind;
  audienceId: string | null; audienceLabel: string; pinned: boolean; requiresAck: boolean; acked: boolean; expiresOn: string | null; canEdit: boolean;
}
export type FeedItem = KudosItem | AnnouncementItem;

export interface Celebration extends Social {
  kind: 'birthday' | 'anniversary' | 'joined'; targetId: string; employeeId: string; name: string; designation: string | null;
  date: string; bsLabel: string; years: number | null; daysAway: number; isMe: boolean;
}
export interface CompanyValue { id: string; name: string; description: string | null; emoji: string; active: boolean; sortOrder: number }
export interface Colleague { id: string; name: string; empCode: string; designation: string | null }
export interface Comment { id: string; body: string; createdAt: string; name: string; canDelete: boolean }
export interface LeaderRow { employeeId: string; name: string; designation: string | null; count: number }

export type QuestionKind = 'rating' | 'enps' | 'single' | 'multi' | 'text';
export interface Question { id: string; kind: QuestionKind; prompt: string; options: string[]; required: boolean }
export interface QuestionDraft { kind: QuestionKind; prompt: string; options: string[]; required: boolean }
export interface SurveySummary {
  id: string; kind: 'survey' | 'poll'; title: string; description: string | null; anonymous: boolean; opensOn: string; closesOn: string;
  questionCount: number; answered: boolean;
}
export interface SurveyFull {
  id: string; kind: 'survey' | 'poll'; title: string; description: string | null; anonymous: boolean; minGroup: number; audienceKind: 'all' | 'branch' | 'department';
  audienceId: string | null; status: 'draft' | 'published' | 'closed'; opensOn: string; closesOn: string; recurrence: 'none' | 'weekly' | 'monthly';
  seriesId: string | null; createdAt: string; publishedAt: string | null; closedAt: string | null; responses?: number; questionCount?: number;
}
export interface QuestionResult {
  questionId: string; kind: QuestionKind; prompt: string; options: string[]; answered: number;
  distribution?: Array<{ label: string; count: number }>; average?: number | null;
  enps?: { score: number; promoters: number; passives: number; detractors: number } | null;
  texts?: Array<{ text: string; name?: string }>; breakdown?: Array<{ group: string; responses: number; average: number | null }>;
}
export interface SurveyResults { responses: number; audience: number; hidden: boolean; hiddenUntilClose?: boolean; minGroup: number; questions: QuestionResult[] }
export interface Answer { questionId: string; value?: number; choices?: string[]; text?: string }
export interface SurveyInput {
  kind: 'survey' | 'poll'; title: string; description?: string | null; anonymous: boolean; minGroup: number; audienceKind: 'all' | 'branch' | 'department';
  audienceId?: string | null; opensOn: string; closesOn: string; recurrence: 'none' | 'weekly' | 'monthly'; questions: QuestionDraft[];
}
export interface Suggestion {
  id: string; category: string; body: string; receivedOn: string; status?: 'new' | 'reviewed' | 'answered' | 'archived'; reply: string | null;
  isPublic?: boolean; repliedAt: string | null;
}
export interface AnnouncementInput {
  title: string; body: string; audienceKind: AudienceKind; audienceId?: string | null; pinned: boolean; requiresAck: boolean; expiresOn?: string | null;
  popup?: boolean; popupUntil?: string | null;
}

export const engage = {
  feed: (q?: Query) => get<{ items: FeedItem[]; nextBefore: string | null }>('/engage/feed', q),
  home: () => get<{ announcement: AnnouncementItem | null; celebrations: Array<{ kind: Celebration['kind']; name: string; employeeId: string; years: number | null; targetId: string }>;
    survey: { id: string; title: string; kind: 'survey' | 'poll'; closesOn: string } | null; surveysWaiting: number }>('/engage/home'),
  announcements: (q?: Query) => get<{ announcements: AnnouncementItem[] }>('/engage/announcements', q),
  postAnnouncement: (b: AnnouncementInput) => post<{ id: string; notified: number }>('/engage/announcements', b),
  editAnnouncement: (id: string, b: Partial<Omit<AnnouncementInput, 'audienceKind' | 'audienceId'>>) => patch<{ ok: true }>(`/engage/announcements/${id}`, b),
  deleteAnnouncement: (id: string) => del<{ ok: true }>(`/engage/announcements/${id}`),
  ack: (id: string) => post<{ ok: true }>(`/engage/announcements/${id}/ack`),
  acks: (id: string) => get<{ audience: number; acked: number; people: Array<{ employeeId: string; name: string; empCode: string; ackedAt: string | null }> }>(`/engage/announcements/${id}/acks`),
  react: (targetType: TargetType, targetId: string, emoji: Reaction) => post<{ reacted: boolean; reactions: Social['reactions']; mine: Reaction[] }>('/engage/reactions', { targetType, targetId, emoji }),
  comments: (targetType: TargetType, targetId: string) => get<{ comments: Comment[] }>('/engage/comments', { targetType, targetId }),
  comment: (targetType: TargetType, targetId: string, body: string) => post<{ id: string }>('/engage/comments', { targetType, targetId, body }),
  deleteComment: (id: string) => del<{ ok: true }>(`/engage/comments/${id}`),
  values: () => get<{ values: CompanyValue[]; kudosMonthlyLimit: number | null }>('/engage/values'),
  createValue: (b: { name: string; description?: string | null; emoji: string }) => post<{ id: string }>('/engage/values', b),
  updateValue: (id: string, b: Partial<{ name: string; description: string | null; emoji: string; active: boolean; sortOrder: number }>) => patch<{ ok: true }>(`/engage/values/${id}`, b),
  saveSettings: (b: { kudosMonthlyLimit: number | null }) => put<{ ok: true }>('/engage/settings', b),
  colleagues: () => get<{ colleagues: Colleague[] }>('/engage/colleagues'),
  giveKudos: (b: { recipientIds: string[]; valueId?: string | null; message: string }) => post<{ id: string }>('/engage/kudos', b),
  deleteKudos: (id: string) => del<{ ok: true }>(`/engage/kudos/${id}`),
  leaderboard: (q?: Query) => get<{ bsYear: number; bsMonth: number; total: number; received: LeaderRow[]; given: LeaderRow[]; values: Array<{ id: string; name: string; emoji: string; count: number }> }>('/engage/leaderboard', q),
  celebrations: (days = 14) => get<{ celebrations: Celebration[] }>('/engage/celebrations', { days }),
  prefs: () => get<{ showBirthday: boolean; hasBirthday: boolean }>('/engage/me/prefs'),
  savePrefs: (showBirthday: boolean) => put<{ ok: true }>('/engage/me/prefs', { showBirthday }),
  surveys: () => get<{ surveys: SurveySummary[] }>('/engage/surveys'),
  manageSurveys: () => get<{ surveys: SurveyFull[] }>('/engage/surveys/manage'),
  survey: (id: string) => get<{ survey: SurveyFull; questions: Question[]; answered: boolean }>(`/engage/surveys/${id}`),
  createSurvey: (b: SurveyInput) => post<{ id: string }>('/engage/surveys', b),
  updateSurvey: (id: string, b: SurveyInput | { closesOn: string }) => put<{ ok: true }>(`/engage/surveys/${id}`, b),
  deleteSurvey: (id: string) => del<{ ok: true }>(`/engage/surveys/${id}`),
  publishSurvey: (id: string) => post<{ notified: number }>(`/engage/surveys/${id}/publish`),
  closeSurvey: (id: string) => post<{ ok: true }>(`/engage/surveys/${id}/close`),
  respond: (id: string, answers: Answer[]) => post<{ ok: true; results?: SurveyResults }>(`/engage/surveys/${id}/respond`, { answers }),
  results: (id: string) => get<{ survey: SurveyFull; results: SurveyResults }>(`/engage/surveys/${id}/results`),
  suggest: (b: { category: string; body: string }) => post<{ ok: true }>('/engage/suggestions', b),
  publicSuggestions: () => get<{ suggestions: Suggestion[] }>('/engage/suggestions/public'),
  suggestions: (q?: Query) => get<{ suggestions: Suggestion[] }>('/engage/suggestions', q),
  updateSuggestion: (id: string, b: { status?: Suggestion['status']; reply?: string; isPublic?: boolean }) => patch<{ ok: true }>(`/engage/suggestions/${id}`, b),
};
