import { Anchor, Text } from '@mantine/core';
import type { ReactNode } from 'react';
import { Link } from 'react-router-dom';
import { fmtDateShort, fmtMoney } from '../../lib/format';

export const EXPENSE_TYPE_LABEL: Record<string, string> = { expense_claim: 'Expense claim', travel_request: 'Travel', travel_settlement: 'Travel settlement' };
export const EXPENSE_TYPE_COLOR: Record<string, string> = { expense_claim: 'teal', travel_request: 'cyan', travel_settlement: 'indigo' };

const m = (v: unknown) => fmtMoney(String(v ?? 0), { decimals: Number(v ?? 0) % 1 === 0 ? 0 : 2 });
const range = (f: unknown, t: unknown) => (String(f) === String(t) ? fmtDateShort(String(f)) : `${fmtDateShort(String(f))} to ${fmtDateShort(String(t))}`);

/** Inbox card text for expense and travel approvals; null for other object types. */
export function expenseSummary(objectType: string, objectId: string, s: Record<string, unknown>): { main: ReactNode; extra: ReactNode[] } | null {
  switch (objectType) {
    case 'expense_claim':
      return {
        main: <>{String(s.title)} <Text span c="dimmed">({m(s.amount)})</Text></>,
        extra: [
          <Text key="i" size="xs" c="dimmed">{String(s.items)} {Number(s.items) === 1 ? 'item' : 'items'}{s.categories ? `: ${String(s.categories)}` : ''}</Text>,
          <Text key="r" size="xs" c="dimmed">{String(s.receipts)} with receipts</Text>,
          <Anchor key="o" size="xs" component={Link} to={`/expenses?open=${objectId}`}>Review lines</Anchor>,
        ],
      };
    case 'travel_request':
      return {
        main: <>Trip to {String(s.destination)}, {range(s.from, s.to)} <Text span c="dimmed">({String(s.days)} {Number(s.days) === 1 ? 'day' : 'days'})</Text></>,
        extra: [
          <Text key="a" size="xs" c="dimmed">Allowance {m(s.dailyAllowance)}</Text>,
          ...(Number(s.advance) > 0 ? [<Text key="v" size="xs" c="orange.7">Advance {m(s.advance)}</Text>] : []),
          ...(s.purpose ? [<Text key="p" size="xs" c="dimmed">“{String(s.purpose)}”</Text>] : []),
          <Anchor key="o" size="xs" component={Link} to={`/expenses/travel?open=${objectId}`}>Details</Anchor>,
        ],
      };
    case 'travel_settlement': {
      const net = Number(s.net);
      return {
        main: <>Settle trip to {String(s.destination)}: {net >= 0 ? `pay ${m(net)}` : `recover ${m(-net)}`}</>,
        extra: [
          <Text key="e" size="xs" c="dimmed">Entitled {m(s.entitlement)}, advance {m(s.advance)}</Text>,
          <Text key="b" size="xs" c="dimmed">{String(s.bills)} {Number(s.bills) === 1 ? 'bill' : 'bills'}</Text>,
        ],
      };
    }
    default:
      return null;
  }
}
