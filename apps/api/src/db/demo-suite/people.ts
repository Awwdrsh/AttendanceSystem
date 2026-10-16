import type { DemoContext, DemoEmployee } from './context.js';
import { addDaysIso, pick } from './kit.js';

const DISTRICTS: Array<[string, string, string]> = [
  ['Bagmati', 'Kathmandu', 'Kathmandu Metropolitan City'], ['Bagmati', 'Lalitpur', 'Lalitpur Metropolitan City'], ['Bagmati', 'Bhaktapur', 'Bhaktapur Municipality'],
  ['Gandaki', 'Kaski', 'Pokhara Metropolitan City'], ['Koshi', 'Morang', 'Biratnagar Metropolitan City'], ['Lumbini', 'Rupandehi', 'Butwal Sub-Metropolitan City'],
  ['Madhesh', 'Dhanusha', 'Janakpur Sub-Metropolitan City'], ['Bagmati', 'Chitwan', 'Bharatpur Metropolitan City'], ['Koshi', 'Sunsari', 'Dharan Sub-Metropolitan City'],
];
const INSTITUTIONS = ['Tribhuvan University', 'Kathmandu University', 'Pokhara University', 'Purbanchal University', 'Islington College', 'Kings College'];
const BLOOD = ['A+', 'B+', 'O+', 'AB+', 'O-', 'A-', 'B-'] as const;
const FATHER = ['Hari', 'Krishna', 'Ram Bahadur', 'Shyam', 'Gopal', 'Bishnu', 'Narayan', 'Dil Bahadur', 'Mohan'];
const MOTHER = ['Sarita', 'Kamala', 'Gita', 'Radha', 'Laxmi', 'Sabitra', 'Mina', 'Durga', 'Parbati'];
const SKILLS: Record<string, string[]> = {
  PRD: ['TypeScript', 'Go', 'Kubernetes', 'PostgreSQL', 'React', 'Linux', 'CI/CD', 'GPU scheduling'],
  FIN: ['Excel', 'Tally', 'Nepal tax (TDS)', 'Financial reporting', 'Budgeting'],
  SAL: ['Enterprise sales', 'CRM', 'Negotiation', 'Customer success', 'Presentations'],
  OPS: ['Office administration', 'Recruiting', 'Vendor management', 'Payroll', 'Facilities'],
};

/** Departments get heads and a couple of teams; every designation in use gets a catalogue entry. */
export async function seedStructure(ctx: DemoContext): Promise<void> {
  const { kit, hr, db, companyId } = ctx;
  const s = await kit.get<{ units: Array<{ id: string; code: string; kind: string; head_employee_id: string | null }>; designations: Array<{ name_en: string }> }>(hr, '/people/structure');
  const unit = (code: string) => s.units.find((u) => u.code === code);
  // The most senior person (earliest joiner) of each department leads it.
  for (const u of s.units.filter((x) => x.kind === 'department' && !x.head_employee_id)) {
    const lead = ctx.employees.filter((e) => e.orgUnitId === u.id).sort((a, b) => (a.joinDate ?? '').localeCompare(b.joinDate ?? ''))[0]
      ?? ctx.employees.find((e) => e.managerId === null);
    if (lead) await kit.patch(hr, `/people/structure/units/${u.id}`, { headEmployeeId: lead.id });
  }
  const eng = unit('PRD') ?? s.units.find((x) => x.kind === 'department');
  if (eng && !unit('PLAT')) {
    const plat = await kit.post<{ unit: { id: string } }>(hr, '/people/structure/units', { kind: 'team', code: 'PLAT', nameEn: 'Platform', parentId: eng.id, description: 'GPU cloud, networking and reliability.' });
    await kit.post(hr, '/people/structure/units', { kind: 'team', code: 'PROD', nameEn: 'Product', parentId: eng.id, description: 'Console, APIs and design.' });
    // Move platform people into the Platform team so the org chart has depth.
    for (const e of ctx.employees.filter((x) => x.orgUnitId === eng.id && /platform|reliability|network|datacenter|backend/i.test(x.designation ?? ''))) {
      await db.query('UPDATE employee_assignment SET org_unit_id = $2 WHERE employee_id = $1 AND valid @> $3::date', [e.id, plat.unit.id, ctx.today]);
    }
  }
  const have = new Set(s.designations.map((d) => d.name_en.toLowerCase()));
  const titles = [...new Set(ctx.employees.map((e) => e.designation).filter((d): d is string => !!d))];
  let n = 0;
  for (const t of titles) {
    if (have.has(t.toLowerCase())) continue;
    const level = /head|manager|lead/i.test(t) ? 7 : /senior/i.test(t) ? 5 : 4;
    const orgUnitId = ctx.employees.find((e) => e.designation === t)?.orgUnitId ?? null;
    n++;
    await kit.post(hr, '/people/structure/designations', { code: `D${String(n).padStart(2, '0')}`, nameEn: t, grade: `G${level}`, jobLevel: level, orgUnitId });
  }
  // Link employees to their catalogue designation and grade.
  await db.query(
    `UPDATE employee e SET designation_id = d.id, grade = coalesce(e.grade, d.grade)
       FROM designation d WHERE d.company_id = e.company_id AND lower(d.name_en) = lower(e.designation) AND e.company_id = $1`, [companyId]);
}

function profileFor(e: DemoEmployee, i: number, today: string) {
  const [province, district, municipality] = pick(DISTRICTS, i * 3 + 1);
  const age = 24 + ((i * 7) % 22);
  const dob = `${Number(today.slice(0, 4)) - age}-${String(1 + ((i * 5) % 12)).padStart(2, '0')}-${String(3 + ((i * 11) % 25)).padStart(2, '0')}`;
  const married = age > 29 && i % 3 !== 0;
  const father = `${pick(FATHER, i)} ${e.last}`;
  const mother = `${pick(MOTHER, i + 2)} ${e.last}`;
  const spouse = married ? `${pick(e.gender === 'female' ? FATHER : MOTHER, i + 4)} ${e.last}` : null;
  const phone = `98${String(41000000 + i * 1379).slice(0, 8)}`;
  return {
    personal: { gender: e.gender ?? (i % 2 ? 'male' : 'female'), dob, maritalStatus: married ? 'couple' : 'single', bloodGroup: pick(BLOOD, i), nationality: 'Nepali' },
    contact: {
      personalEmail: `${e.first.toLowerCase()}.${e.last.toLowerCase().replace(/[^a-z]/g, '')}${i}@gmail.com`, personalPhone: phone,
      permanentAddress: { province, district, municipality, ward: 1 + ((i * 3) % 18) },
      temporaryAddress: { province: 'Bagmati', district: pick(['Kathmandu', 'Lalitpur', 'Bhaktapur'], i), municipality: pick(['Kathmandu Metropolitan City', 'Lalitpur Metropolitan City', 'Madhyapur Thimi Municipality'], i), ward: 2 + (i % 30), street: pick(['Baneshwor', 'Jhamsikhel', 'Kupondole', 'Baluwatar', 'Thamel', 'Sanepa', 'Maharajgunj'], i) },
    },
    emergency: { contacts: [{ name: spouse ?? father, relation: spouse ? 'Spouse' : 'Father', phone: `98${String(51000000 + i * 977).slice(0, 8)}` }] },
    family: {
      members: [
        { name: father, relation: 'father', occupation: pick(['Farmer', 'Retired teacher', 'Business', 'Civil servant'], i), isNominee: !spouse, nomineeShare: spouse ? null : 50 },
        { name: mother, relation: 'mother', occupation: pick(['Homemaker', 'Teacher', 'Business'], i), isNominee: !spouse, nomineeShare: spouse ? null : 50 },
        ...(spouse ? [{ name: spouse, relation: 'spouse', isNominee: true, nomineeShare: 100, isDependent: false }] : []),
      ],
    },
    education: {
      items: [
        { level: 'see', institution: pick(['Budhanilkantha School', 'St. Xavier\'s School', 'Gyanodaya School', 'Little Angels\' School', 'Shree Janata Secondary School'], i), board: 'NEB', endYear: Number(dob.slice(0, 4)) + 16, result: `GPA ${(3.2 + (i % 8) / 10).toFixed(2)}` },
        { level: 'bachelor', degree: pick(['BE Computer', 'BBA', 'BSc CSIT', 'BBS', 'BIM', 'BE Electronics'], i), institution: pick(INSTITUTIONS, i), endYear: Number(dob.slice(0, 4)) + 22, result: 'First division' },
        ...(i % 4 === 0 ? [{ level: 'master', degree: pick(['MBA', 'MSc Computer Science', 'MBS'], i), institution: pick(INSTITUTIONS, i + 1), endYear: Number(dob.slice(0, 4)) + 25 }] : []),
      ],
    },
    experience: {
      items: i % 3 === 0 ? [] : [{
        organisation: pick(['Leapfrog Technology', 'F1Soft International', 'Cloud Factory', 'Ncell', 'Nabil Bank', 'Daraz Nepal', 'Fusemachines'], i),
        designation: pick(['Associate', 'Engineer', 'Officer', 'Executive'], i), fromDate: addDaysIso(e.joinDate ?? today, -(900 + i * 40)), toDate: addDaysIso(e.joinDate ?? today, -20),
        reasonForLeaving: pick(['Career growth', 'Better role', 'Relocation to Kathmandu'], i),
      }],
    },
    bank: { bankCode: pick(['NABIL', 'NICA', 'GLOBAL', 'HBL', 'SCB'], i), bankBranch: pick(['Kamaladi', 'Baluwatar', 'New Road', 'Thamel', 'Pulchowk'], i), bankAccount: `0${String(1010010000000 + i * 7713)}` },
    skills: { skills: (SKILLS[e.orgCode ?? 'OPS'] ?? SKILLS.OPS!).filter((_, k) => (k + i) % 2 === 0).slice(0, 5), languages: ['Nepali', 'English', ...(i % 3 === 0 ? ['Hindi'] : []), ...(i % 5 === 0 ? ['Newari'] : [])] },
    ids: {
      pan: String(301000000 + i * 7919).slice(0, 9),
      citizenshipNo: `${String(10 + (i % 70)).padStart(2, '0')}-01-${String(70 + (i % 9))}-${String(10000 + i * 731).slice(0, 5)}`, citizenshipDistrict: district,
      ssfId: `SSF${String(5000000 + i * 1373).slice(0, 7)}`,
      ...(i % 4 === 1 ? { passportNo: `PA${String(1200000 + i * 917).slice(0, 7)}`, passportExpiry: addDaysIso(today, i === 1 ? 45 : 900 + i * 30) } : {}),
      ...(i % 3 === 2 ? { licenceNo: `01-06-${String(400000 + i * 271).slice(0, 6)}`, licenceExpiry: addDaysIso(today, i === 2 ? -10 : 600 + i * 20) } : {}),
    },
  };
}

/** Full profiles for everyone: personal, contact, emergency, family and nominees, education, experience, skills and IDs. */
export async function seedProfiles(ctx: DemoContext): Promise<void> {
  const { kit, hr } = ctx;
  for (const [i, e] of ctx.employees.entries()) {
    const p = profileFor(e, i, ctx.today);
    // PAN and bank details already on file (payroll may use them) are left as they are.
    const has = (await ctx.db.query<{ pan: boolean; bank: boolean }>('SELECT pan_enc IS NOT NULL AS pan, bank_account_enc IS NOT NULL AS bank FROM employee WHERE id = $1', [e.id])).rows[0];
    if (has?.pan) delete (p.ids as { pan?: string }).pan;
    for (const section of ['personal', 'contact', 'emergency', 'family', 'education', 'experience', 'skills', 'ids', 'bank'] as const) {
      if (section === 'bank' && has?.bank) continue;
      await kit.put(hr, `/people/${e.id}/profile/${section}`, p[section]);
    }
  }
}

/** Career history: promotions, a transfer and a confirmation in the past, and one promotion scheduled next month. */
export async function seedCareer(ctx: DemoContext): Promise<void> {
  const { kit, hr } = ctx;
  const s = await kit.get<{ units: Array<{ id: string; code: string }> }>(hr, '/people/structure');
  const staff = ctx.employees.filter((e) => e.managerId);
  const a = staff[0]; const b = staff[3]; const c = staff[5]; const d = staff[7];
  if (a?.joinDate) await kit.post(hr, `/people/${a.id}/career`, { kind: 'confirmation', effectiveDate: addDaysIso(a.joinDate, 180), note: 'Confirmed after six months of probation.' });
  if (b?.joinDate) {
    await kit.post(hr, `/people/${b.id}/career`, { kind: 'confirmation', effectiveDate: addDaysIso(b.joinDate, 180) });
    await kit.post(hr, `/people/${b.id}/career`, { kind: 'grade_change', effectiveDate: addDaysIso(ctx.today, -40), details: { grade: 'G5', reason: 'Annual review 2082/83' }, note: 'Annual review outcome.' });
  }
  if (c?.joinDate) {
    const plat = s.units.find((u) => u.code === 'PLAT');
    if (plat && c.orgUnitId !== plat.id) await kit.post(hr, `/people/${c.id}/career`, { kind: 'transfer', effectiveDate: addDaysIso(ctx.today, -20), details: { orgUnitId: plat.id, reason: 'Moved to the Platform team' } });
  }
  if (d?.joinDate) {
    await kit.post(hr, `/people/${d.id}/career`, { kind: 'promotion', effectiveDate: addDaysIso(ctx.today, 30), details: { designation: `Senior ${d.designation ?? 'Engineer'}`.replace('Senior Senior', 'Senior'), grade: 'G6', revisionPercent: 12 }, note: 'Promotion agreed in the mid-year review.' });
  }
}
