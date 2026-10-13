import { readdirSync, readFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';
import { computePayslip } from './compute.js';
import type { PayrollInput } from './types.js';

const dir = path.join(path.dirname(fileURLToPath(import.meta.url)), 'golden');
const files = readdirSync(dir).filter((f) => f.endsWith('.json')).sort();

/** Golden fixtures (plan §11.11): every case must reproduce exactly; any diff fails the build. */
describe('golden payroll fixtures', () => {
  it('has at least 20 fixtures', () => { expect(files.length).toBeGreaterThanOrEqual(20); });
  for (const f of files) {
    it(f, () => {
      const fx = JSON.parse(readFileSync(path.join(dir, f), 'utf8')) as { input: PayrollInput; expected: Record<string, unknown> & { lines: unknown[] } };
      const r = computePayslip(fx.input);
      expect({ gross: r.gross, taxable: r.taxable, tds: r.tds, employeeStatutory: r.employeeStatutory, employerStatutory: r.employerStatutory, net: r.net,
        annualTax: r.tax.annualTax, lines: r.lines.map((l) => ({ code: l.code, type: l.type, amount: l.amount })), flags: r.flags }).toEqual(fx.expected);
    });
  }
});
