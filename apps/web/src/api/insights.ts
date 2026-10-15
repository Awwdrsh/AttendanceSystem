import { download, get, type Query } from './client';

interface MonthRow { month: string; label: string; short: string }

export interface InsightsData {
  months: Array<{ key: string; label: string; short: string; startAd: string; endAd: string }>;
  asOf: string;
  kpis: Array<{ key: string; label: string; value: number | null; delta: number | null; unit: string; goodWhen: 'up' | 'down' | 'neutral'; vs?: string }>;
  headcount: Array<MonthRow & { headcount: number; joins: number; exits: number; net: number; attritionPct: number; voluntary: number; involuntary: number }>;
  attrition: { exits: number; monthlyAvgPct: number; annualisedPct: number; split: { voluntary: number; involuntary: number; other: number } | null };
  tenure: Array<{ label: string; count: number }>;
  age: Array<{ label: string; count: number }>;
  gender: Array<{ label: string; count: number }>;
  departments: Array<{ label: string; count: number }>;
  branches: Array<{ label: string; count: number }>;
  cost: Array<MonthRow & { gross: number; employerContributions: number; totalCost: number; net: number; tds: number; people: number; locked: boolean }>;
  costByDepartment: Array<{ label: string; totalCost: number }>;
  overtime: Array<MonthRow & { detectedHours: number; approvedHours: number }>;
  leave: Array<{ type: string; code: string; days: number; perEmployee: number }>;
  leaveMonthly: Array<MonthRow & { days: number }>;
  attendance: Array<MonthRow & { scheduledDays: number; presentDays: number; absentDays: number; lateDays: number; absenteeismPct: number | null; latePct: number | null; avgWorkedHours: number | null }>;
  exitReasonsAvailable: boolean;
}

export type InsightsDataset = 'headcount' | 'tenure' | 'age' | 'gender' | 'departments' | 'branches' | 'cost' | 'costByDepartment' | 'overtime' | 'leave' | 'attendance';

export const insights = {
  get: (q?: Query) => get<InsightsData>('/insights', q),
  filters: () => get<{ branches: Array<{ id: string; name: string }>; orgUnits: Array<{ id: string; name: string; kind: string; parentId: string | null }> }>('/insights/filters'),
  exportCsv: (dataset: InsightsDataset, q: Query) => {
    const p = new URLSearchParams();
    for (const [k, v] of Object.entries(q)) if (v !== undefined && v !== null && v !== '') p.set(k, String(v));
    const s = p.toString();
    return download(`/insights/export/${dataset}${s ? `?${s}` : ''}`, `insights-${dataset}.csv`);
  },
};
