"""Structure, profiles and career history (port of apps/api/src/db/demo-suite/people.ts)."""
from __future__ import annotations

import re

from hajiri.core.db import execute, one
from hajiri.people import service as people

from .context import DemoContext, DemoEmployee
from .kit import add_days_iso, pick

DISTRICTS = [
    ('Bagmati', 'Kathmandu', 'Kathmandu Metropolitan City'), ('Bagmati', 'Lalitpur', 'Lalitpur Metropolitan City'), ('Bagmati', 'Bhaktapur', 'Bhaktapur Municipality'),
    ('Gandaki', 'Kaski', 'Pokhara Metropolitan City'), ('Koshi', 'Morang', 'Biratnagar Metropolitan City'), ('Lumbini', 'Rupandehi', 'Butwal Sub-Metropolitan City'),
    ('Madhesh', 'Dhanusha', 'Janakpur Sub-Metropolitan City'), ('Bagmati', 'Chitwan', 'Bharatpur Metropolitan City'), ('Koshi', 'Sunsari', 'Dharan Sub-Metropolitan City'),
]
INSTITUTIONS = ['Tribhuvan University', 'Kathmandu University', 'Pokhara University', 'Purbanchal University', 'Islington College', 'Kings College']
BLOOD = ['A+', 'B+', 'O+', 'AB+', 'O-', 'A-', 'B-']
FATHER = ['Hari', 'Krishna', 'Ram Bahadur', 'Shyam', 'Gopal', 'Bishnu', 'Narayan', 'Dil Bahadur', 'Mohan']
MOTHER = ['Sarita', 'Kamala', 'Gita', 'Radha', 'Laxmi', 'Sabitra', 'Mina', 'Durga', 'Parbati']
SKILLS = {
    'PRD': ['TypeScript', 'Go', 'Kubernetes', 'PostgreSQL', 'React', 'Linux', 'CI/CD', 'GPU scheduling'],
    'FIN': ['Excel', 'Tally', 'Nepal tax (TDS)', 'Financial reporting', 'Budgeting'],
    'SAL': ['Enterprise sales', 'CRM', 'Negotiation', 'Customer success', 'Presentations'],
    'OPS': ['Office administration', 'Recruiting', 'Vendor management', 'Payroll', 'Facilities'],
}


def structure(ctx: DemoContext) -> dict:
    return ctx.kit.call('GET /people/structure', people.structure, ctx.hr.user)


def seed_structure(ctx: DemoContext) -> None:
    """Departments get heads and a couple of teams; every designation in use gets a catalogue entry."""
    kit, hr = ctx.kit, ctx.hr
    s = structure(ctx)

    def unit(code: str):
        return next((u for u in s['units'] if u['code'] == code), None)
    # The most senior person (earliest joiner) of each department leads it.
    for u in [x for x in s['units'] if x['kind'] == 'department' and not x['head_employee_id']]:
        members = sorted([e for e in ctx.employees if e.org_unit_id == u['id']], key=lambda e: e.join_date or '')
        lead = members[0] if members else next((e for e in ctx.employees if e.manager_id is None), None)
        if lead:
            kit.call(f"PATCH /people/structure/units/{u['id']}", people.update_unit, hr.user, u['id'], {'headEmployeeId': lead.id})
    eng = unit('PRD') or next((x for x in s['units'] if x['kind'] == 'department'), None)
    if eng and not unit('PLAT'):
        plat = kit.call('POST /people/structure/units', people.create_unit, hr.user,
                        {'kind': 'team', 'code': 'PLAT', 'nameEn': 'Platform', 'parentId': eng['id'], 'description': 'GPU cloud, networking and reliability.'})
        kit.call('POST /people/structure/units', people.create_unit, hr.user,
                 {'kind': 'team', 'code': 'PROD', 'nameEn': 'Product', 'parentId': eng['id'], 'description': 'Console, APIs and design.'})
        # Move platform people into the Platform team so the org chart has depth.
        for e in [x for x in ctx.employees if x.org_unit_id == eng['id']
                  and re.search(r'platform|reliability|network|datacenter|backend', x.designation or '', re.I)]:
            execute('UPDATE employee_assignment SET org_unit_id = %s WHERE employee_id = %s AND valid @> %s::date', [plat['id'], e.id, ctx.today])
    have = {d['name_en'].lower() for d in s['designations']}
    titles = list(dict.fromkeys(e.designation for e in ctx.employees if e.designation))
    n = 0
    for t in titles:
        if t.lower() in have:
            continue
        level = 7 if re.search(r'head|manager|lead', t, re.I) else 5 if re.search(r'senior', t, re.I) else 4
        org_unit_id = next((e.org_unit_id for e in ctx.employees if e.designation == t), None)
        n += 1
        kit.call('POST /people/structure/designations', people.create_designation, hr.user,
                 {'code': f'D{n:02d}', 'nameEn': t, 'grade': f'G{level}', 'jobLevel': level, 'orgUnitId': org_unit_id})
    # Link employees to their catalogue designation and grade.
    execute("""UPDATE employee e SET designation_id = d.id, grade = coalesce(e.grade, d.grade)
                 FROM designation d WHERE d.company_id = e.company_id AND lower(d.name_en) = lower(e.designation) AND e.company_id = %s""",
            [ctx.company_id])


def _gpa(i: int) -> str:
    return f'{3.2 + (i % 8) / 10:.2f}'


def profile_for(e: DemoEmployee, i: int, today: str) -> dict:
    province, district, municipality = pick(DISTRICTS, i * 3 + 1)
    age = 24 + ((i * 7) % 22)
    dob = f'{int(today[:4]) - age}-{1 + ((i * 5) % 12):02d}-{3 + ((i * 11) % 25):02d}'
    married = age > 29 and i % 3 != 0
    father = f'{pick(FATHER, i)} {e.last}'
    mother = f'{pick(MOTHER, i + 2)} {e.last}'
    spouse = f"{pick(FATHER if e.gender == 'female' else MOTHER, i + 4)} {e.last}" if married else None
    phone = f'98{str(41000000 + i * 1379)[:8]}'
    birth_year = int(dob[:4])
    ids: dict = {
        'pan': str(301000000 + i * 7919)[:9],
        'citizenshipNo': f'{10 + (i % 70):02d}-01-{70 + (i % 9)}-{str(10000 + i * 731)[:5]}', 'citizenshipDistrict': district,
        'ssfId': f'SSF{str(5000000 + i * 1373)[:7]}',
    }
    if i % 4 == 1:
        ids.update({'passportNo': f'PA{str(1200000 + i * 917)[:7]}', 'passportExpiry': add_days_iso(today, 45 if i == 1 else 900 + i * 30)})
    if i % 3 == 2:
        ids.update({'licenceNo': f'01-06-{str(400000 + i * 271)[:6]}', 'licenceExpiry': add_days_iso(today, -10 if i == 2 else 600 + i * 20)})
    members = [
        {'name': father, 'relation': 'father', 'occupation': pick(['Farmer', 'Retired teacher', 'Business', 'Civil servant'], i),
         'isNominee': not spouse, 'nomineeShare': None if spouse else 50},
        {'name': mother, 'relation': 'mother', 'occupation': pick(['Homemaker', 'Teacher', 'Business'], i),
         'isNominee': not spouse, 'nomineeShare': None if spouse else 50},
    ]
    if spouse:
        members.append({'name': spouse, 'relation': 'spouse', 'isNominee': True, 'nomineeShare': 100, 'isDependent': False})
    education = [
        {'level': 'see', 'institution': pick(['Budhanilkantha School', "St. Xavier's School", 'Gyanodaya School', "Little Angels' School",
                                              'Shree Janata Secondary School'], i), 'board': 'NEB', 'endYear': birth_year + 16, 'result': f'GPA {_gpa(i)}'},
        {'level': 'bachelor', 'degree': pick(['BE Computer', 'BBA', 'BSc CSIT', 'BBS', 'BIM', 'BE Electronics'], i),
         'institution': pick(INSTITUTIONS, i), 'endYear': birth_year + 22, 'result': 'First division'},
    ]
    if i % 4 == 0:
        education.append({'level': 'master', 'degree': pick(['MBA', 'MSc Computer Science', 'MBS'], i), 'institution': pick(INSTITUTIONS, i + 1),
                          'endYear': birth_year + 25})
    experience = [] if i % 3 == 0 else [{
        'organisation': pick(['Leapfrog Technology', 'F1Soft International', 'Cloud Factory', 'Ncell', 'Nabil Bank', 'Daraz Nepal', 'Fusemachines'], i),
        'designation': pick(['Associate', 'Engineer', 'Officer', 'Executive'], i),
        'fromDate': add_days_iso(e.join_date or today, -(900 + i * 40)), 'toDate': add_days_iso(e.join_date or today, -20),
        'reasonForLeaving': pick(['Career growth', 'Better role', 'Relocation to Kathmandu'], i),
    }]
    skills = SKILLS.get(e.org_code or 'OPS') or SKILLS['OPS']
    return {
        'personal': {'gender': e.gender or ('male' if i % 2 else 'female'), 'dob': dob, 'maritalStatus': 'couple' if married else 'single',
                     'bloodGroup': pick(BLOOD, i), 'nationality': 'Nepali'},
        'contact': {
            'personalEmail': f"{e.first.lower()}.{re.sub(r'[^a-z]', '', e.last.lower())}{i}@gmail.com", 'personalPhone': phone,
            'permanentAddress': {'province': province, 'district': district, 'municipality': municipality, 'ward': 1 + ((i * 3) % 18)},
            'temporaryAddress': {'province': 'Bagmati', 'district': pick(['Kathmandu', 'Lalitpur', 'Bhaktapur'], i),
                                 'municipality': pick(['Kathmandu Metropolitan City', 'Lalitpur Metropolitan City', 'Madhyapur Thimi Municipality'], i),
                                 'ward': 2 + (i % 30), 'street': pick(['Baneshwor', 'Jhamsikhel', 'Kupondole', 'Baluwatar', 'Thamel', 'Sanepa', 'Maharajgunj'], i)},
        },
        'emergency': {'contacts': [{'name': spouse or father, 'relation': 'Spouse' if spouse else 'Father', 'phone': f'98{str(51000000 + i * 977)[:8]}'}]},
        'family': {'members': members},
        'education': {'items': education},
        'experience': {'items': experience},
        'bank': {'bankCode': pick(['NABIL', 'NICA', 'GLOBAL', 'HBL', 'SCB'], i), 'bankBranch': pick(['Kamaladi', 'Baluwatar', 'New Road', 'Thamel', 'Pulchowk'], i),
                 'bankAccount': f'0{1010010000000 + i * 7713}'},
        'skills': {'skills': [s for k, s in enumerate(skills) if (k + i) % 2 == 0][:5],
                   'languages': ['Nepali', 'English', *(['Hindi'] if i % 3 == 0 else []), *(['Newari'] if i % 5 == 0 else [])]},
        'ids': ids,
    }


def seed_profiles(ctx: DemoContext) -> None:
    """Full profiles for everyone: personal, contact, emergency, family and nominees, education, experience, skills and IDs."""
    for i, e in enumerate(ctx.employees):
        p = profile_for(e, i, ctx.today)
        # PAN and bank details already on file (payroll may use them) are left as they are.
        has = one('SELECT pan_enc IS NOT NULL AS pan, bank_account_enc IS NOT NULL AS bank FROM employee WHERE id = %s', [e.id])
        if has and has['pan']:
            p['ids'].pop('pan', None)
        for section in ('personal', 'contact', 'emergency', 'family', 'education', 'experience', 'skills', 'ids', 'bank'):
            if section == 'bank' and has and has['bank']:
                continue
            ctx.kit.call(f'PUT /people/{e.id}/profile/{section}', people.save_section, ctx.hr.user, e.id, section, p[section])


def seed_career(ctx: DemoContext) -> None:
    """Career history: promotions, a transfer and a confirmation in the past, and one promotion scheduled next month."""
    kit, hr = ctx.kit, ctx.hr
    s = structure(ctx)
    staff = [e for e in ctx.employees if e.manager_id]

    def at(k: int):
        return staff[k] if k < len(staff) else None
    a, b, c, d = at(0), at(3), at(5), at(7)

    def career(e: DemoEmployee, body: dict) -> None:
        kit.call(f'POST /people/{e.id}/career', people.record_career, hr.user, e.id, body)
    if a and a.join_date:
        career(a, {'kind': 'confirmation', 'effectiveDate': add_days_iso(a.join_date, 180), 'note': 'Confirmed after six months of probation.'})
    if b and b.join_date:
        career(b, {'kind': 'confirmation', 'effectiveDate': add_days_iso(b.join_date, 180)})
        career(b, {'kind': 'grade_change', 'effectiveDate': add_days_iso(ctx.today, -40), 'details': {'grade': 'G5', 'reason': 'Annual review 2082/83'},
                   'note': 'Annual review outcome.'})
    if c and c.join_date:
        plat = next((u for u in s['units'] if u['code'] == 'PLAT'), None)
        if plat and c.org_unit_id != plat['id']:
            career(c, {'kind': 'transfer', 'effectiveDate': add_days_iso(ctx.today, -20), 'details': {'orgUnitId': plat['id'], 'reason': 'Moved to the Platform team'}})
    if d and d.join_date:
        career(d, {'kind': 'promotion', 'effectiveDate': add_days_iso(ctx.today, 30),
                   'details': {'designation': f"Senior {d.designation or 'Engineer'}".replace('Senior Senior', 'Senior', 1), 'grade': 'G6', 'revisionPercent': 12},
                   'note': 'Promotion agreed in the mid-year review.'})
