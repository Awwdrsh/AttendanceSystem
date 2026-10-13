import { D } from '@hajiri/payroll-engine';

export interface BankRow { empCode: string; name: string; account: string; branch: string; amount: string; bankCode: string; mobile?: string; email?: string }
export interface BankFile { bankCode: string; format: string; filename: string; content: string; errors: string[] }

const ascii = (s: string) => s.normalize('NFKD').replace(/[^\x20-\x7E]/g, '').trim();
const csvCell = (s: string) => (/[",\n]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s);

/** Per-bank bulk salary formats (plan §6.9; single vendor source — confirm each layout with the bank before first use). */
export function buildBankFile(bankCode: string, rows: BankRow[], period: string): BankFile {
  const errors: string[] = [];
  for (const r of rows) {
    if (!r.account) errors.push(`${r.empCode}: no bank account on file`);
    if (!D(r.amount).gt(0)) errors.push(`${r.empCode}: amount must be positive`);
  }
  const amt = (a: string) => D(a).toFixed(2);
  switch (bankCode) {
    case 'NMB': {
      const lines = rows.map((r) => [r.account, csvCell(ascii(r.name)), amt(r.amount), r.branch.padStart(3, '0').slice(-3), `Salary ${period}`].join(','));
      return { bankCode, format: 'csv', filename: `nmb-salary-${period}.csv`, content: lines.join('\r\n') + '\r\n', errors };
    }
    case 'NABIL': {
      const lines = rows.map((r) => [r.account, ascii(r.name).slice(0, 50), amt(r.amount), r.branch, `SAL${period.replace(/\D/g, '')}`.slice(0, 12)].join('\t'));
      return { bankCode, format: 'tsv', filename: `nabil-salary-${period}.tsv`, content: lines.join('\r\n') + '\r\n', errors };
    }
    case 'NIC_ASIA': {
      const lines = ['branch,account,name,amount,mobile,email', ...rows.map((r) => [r.branch, r.account, csvCell(ascii(r.name)), amt(r.amount), r.mobile ?? '', r.email ?? ''].join(','))];
      return { bankCode, format: 'csv', filename: `nicasia-salary-${period}.csv`, content: lines.join('\r\n') + '\r\n', errors };
    }
    case 'EVEREST': {
      // Fixed width 80: account 14 | name 35 | amount 15 implied 2 dp | branch 4 | reference 12; ASCII, no header.
      const lines = rows.map((r) => {
        const name = ascii(r.name);
        if (name !== r.name.trim()) errors.push(`${r.empCode}: name contains non-ASCII characters; transliterated`);
        const paisa = D(r.amount).times(100).toFixed(0);
        return r.account.padEnd(14).slice(0, 14) + name.padEnd(35).slice(0, 35) + paisa.padStart(15, '0').slice(-15) + r.branch.padStart(4, '0').slice(-4) + `SAL${period.replace(/\D/g, '')}`.padEnd(12).slice(0, 12);
      });
      return { bankCode, format: 'txt', filename: `everest-salary-${period}.txt`, content: lines.join('\r\n') + '\r\n', errors };
    }
    case 'PRABHU': {
      const lines = rows.map((r) => [r.account, csvCell(ascii(r.name)), amt(r.amount), `Salary ${period}`].join(','));
      return { bankCode, format: 'csv', filename: `prabhu-salary-${period}.csv`, content: lines.join('\r\n') + '\r\n', errors };
    }
    default: {
      const lines = ['emp_code,name,bank_code,bank_branch,account,amount', ...rows.map((r) => [r.empCode, csvCell(r.name), r.bankCode, r.branch, r.account, amt(r.amount)].join(','))];
      return { bankCode: 'GENERIC', format: 'csv', filename: `salary-${period}.csv`, content: lines.join('\r\n') + '\r\n', errors };
    }
  }
}
