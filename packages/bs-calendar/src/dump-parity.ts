// Writes every BS day and fiscal year as JSON so the Python port (server/engines/bs_calendar) can be checked against it.
import { writeFileSync } from 'node:fs';
import { allSupportedDays, fiscalYear, supportedBsYears } from './index.js';

const days: string[] = [];
for (const { bs, ad } of allSupportedDays()) days.push(`${bs.year}-${bs.month}-${bs.day}=${ad}`);
const { min, max } = supportedBsYears();
const fys = [];
for (let y = min; y <= max; y++) fys.push(fiscalYear(y));
writeFileSync(process.argv[2]!, JSON.stringify({ days, fiscalYears: fys }));
