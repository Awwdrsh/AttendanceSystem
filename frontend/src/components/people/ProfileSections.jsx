import {
  ActionIcon,
  Alert,
  Badge,
  Button,
  Checkbox,
  Divider,
  Drawer,
  Group,
  NumberInput,
  Paper,
  Select,
  SimpleGrid,
  Skeleton,
  Stack,
  TagsInput,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core';
import { IconEye, IconLock, IconPencil, IconPlus, IconSend, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';
import { people, SECTION_LABEL } from '../../api/people';
import { fmtAd } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { DateField } from '../BsDatePicker';
import { EmptyState } from '../EmptyState';
import { FileUpload } from '../FileUpload';
import { Field } from '../Kpi';

const ICON = { size: 16, stroke: 1.6 };
const ORDER = ['personal', 'contact', 'emergency', 'family', 'ids', 'bank', 'education', 'experience', 'skills'];
const SECRET = new Set(['pan', 'bankAccount', 'citizenshipNo', 'passportNo', 'licenceNo']);

const GENDER = [
  { value: 'male', label: 'Male' },
  { value: 'female', label: 'Female' },
  { value: 'other', label: 'Other' },
];
const MARITAL = [
  { value: 'single', label: 'Single' },
  { value: 'couple', label: 'Married' },
];
const BLOOD = ['A+', 'A-', 'B+', 'B-', 'AB+', 'AB-', 'O+', 'O-'];
const PROVINCES = ['Koshi', 'Madhesh', 'Bagmati', 'Gandaki', 'Lumbini', 'Karnali', 'Sudurpashchim'];
const RELATIONS = [
  ['spouse', 'Spouse'],
  ['father', 'Father'],
  ['mother', 'Mother'],
  ['son', 'Son'],
  ['daughter', 'Daughter'],
  ['brother', 'Brother'],
  ['sister', 'Sister'],
  ['father_in_law', 'Father-in-law'],
  ['mother_in_law', 'Mother-in-law'],
  ['grandfather', 'Grandfather'],
  ['grandmother', 'Grandmother'],
  ['other', 'Other'],
].map(([value, label]) => ({ value: value, label: label }));
const LEVELS = [
  ['see', 'SEE / SLC'],
  ['plus_two', '+2 / Intermediate'],
  ['diploma', 'Diploma'],
  ['bachelor', "Bachelor's"],
  ['master', "Master's"],
  ['mphil', 'MPhil'],
  ['phd', 'PhD'],
  ['other', 'Other'],
].map(([value, label]) => ({ value: value, label: label }));
const label = (opts, v) => (v ? (opts.find((o) => o.value === v)?.label ?? v) : '—');

function addressText(a) {
  if (!a) return '—';
  return (
    [a.street, a.ward ? `Ward ${a.ward}` : null, a.municipality, a.district, a.province ? `${a.province} Province` : null]
      .filter(Boolean)
      .join(', ') || '—'
  );
}

/** Read-only view of one section. */
function SectionView({ section, p }) {
  switch (section) {
    case 'personal':
      return (
        <SimpleGrid cols={{ base: 1, xs: 2, md: 3 }} spacing="md">
          <Field label="Gender">{label(GENDER, p.personal.gender)}</Field>
          <Field label="Date of birth">{fmtAd(p.personal.dob)}</Field>
          <Field label="Marital status">{label(MARITAL, p.personal.maritalStatus)}</Field>
          <Field label="Blood group">{p.personal.bloodGroup ?? '—'}</Field>
          <Field label="Nationality">{p.personal.nationality ?? '—'}</Field>
        </SimpleGrid>
      );
    case 'contact':
      return (
        <SimpleGrid cols={{ base: 1, xs: 2, md: 3 }} spacing="md">
          <Field label="Personal email">{p.contact.personalEmail ?? '—'}</Field>
          <Field label="Personal phone">{p.contact.personalPhone ?? '—'}</Field>
          <Field label="Work phone">{p.contact.workPhone ?? '—'}</Field>
          <Field label="Permanent address">{addressText(p.contact.permanentAddress)}</Field>
          <Field label="Temporary address">{addressText(p.contact.temporaryAddress)}</Field>
        </SimpleGrid>
      );
    case 'bank':
      return (
        <SimpleGrid cols={{ base: 1, xs: 2, md: 3 }} spacing="md">
          <Field label="Bank">{p.bank.bankCode ?? '—'}</Field>
          <Field label="Branch">{p.bank.bankBranch ?? '—'}</Field>
          <Field label="Account number">{p.bank.bankAccount ?? '—'}</Field>
        </SimpleGrid>
      );
    case 'ids':
      return (
        <SimpleGrid cols={{ base: 1, xs: 2, md: 3 }} spacing="md">
          <Field label="PAN">{p.ids.pan ?? '—'}</Field>
          <Field label="Citizenship number">{p.ids.citizenshipNo ?? '—'}</Field>
          <Field label="Issued in">
            {[p.ids.citizenshipDistrict, p.ids.citizenshipIssuedOn ? fmtAd(p.ids.citizenshipIssuedOn) : null].filter(Boolean).join(', ') ||
              '—'}
          </Field>
          <Field label="Passport">
            {p.ids.passportNo ? `${p.ids.passportNo}${p.ids.passportExpiry ? `, expires ${fmtAd(p.ids.passportExpiry)}` : ''}` : '—'}
          </Field>
          <Field label="Driving licence">
            {p.ids.licenceNo ? `${p.ids.licenceNo}${p.ids.licenceExpiry ? `, expires ${fmtAd(p.ids.licenceExpiry)}` : ''}` : '—'}
          </Field>
          <Field label="SSF ID">{p.ids.ssfId ?? '—'}</Field>
          <Field label="CIT ID">{p.ids.citId ?? '—'}</Field>
          <Field label="PF number">{p.ids.pfId ?? '—'}</Field>
        </SimpleGrid>
      );
    case 'emergency':
      return p.emergency.contacts.length ? (
        <Stack gap="xs">
          {p.emergency.contacts.map((c, i) => (
            <Group key={i} justify="space-between" wrap="wrap" gap="xs">
              <Text size="sm" fw={500}>
                {c.name}{' '}
                <Text span c="dimmed" size="sm">
                  ({c.relation})
                </Text>
              </Text>
              <Text size="sm" className="num">
                {[c.phone, c.altPhone].filter(Boolean).join(', ')}
              </Text>
            </Group>
          ))}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">
          No emergency contact yet. Add at least one.
        </Text>
      );
    case 'family':
      return p.family.members.length ? (
        <Stack gap="xs">
          {p.family.members.map((m, i) => (
            <Group key={i} justify="space-between" wrap="wrap" gap="xs">
              <Text size="sm" fw={500}>
                {m.name}{' '}
                <Text span c="dimmed" size="sm">
                  ({label(RELATIONS, m.relation)}
                  {m.dob ? `, born ${fmtAd(m.dob)}` : ''})
                </Text>
              </Text>
              <Group gap={6}>
                {m.isDependent ? (
                  <Badge size="sm" variant="light" color="gray">
                    Dependent
                  </Badge>
                ) : null}
                {m.isNominee ? (
                  <Badge size="sm" variant="light" color="brand">
                    Nominee {m.nomineeShare}%
                  </Badge>
                ) : null}
              </Group>
            </Group>
          ))}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">
          No family members recorded. Nominees for SSF and gratuity are chosen here.
        </Text>
      );
    case 'education':
      return p.education.items.length ? (
        <Stack gap="xs">
          {p.education.items.map((e, i) => (
            <div key={i}>
              <Text size="sm" fw={500}>
                {[label(LEVELS, e.level), e.degree, e.field].filter((x) => x && x !== '—').join(', ')}
              </Text>
              <Text size="xs" c="dimmed">
                {[e.institution, e.board, [e.startYear, e.endYear].filter(Boolean).join('–'), e.result].filter(Boolean).join(' · ')}
              </Text>
            </div>
          ))}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">
          No education recorded.
        </Text>
      );
    case 'experience':
      return p.experience.items.length ? (
        <Stack gap="xs">
          {p.experience.items.map((e, i) => (
            <div key={i}>
              <Text size="sm" fw={500}>
                {[e.designation, e.organisation].filter(Boolean).join(' at ')}
              </Text>
              <Text size="xs" c="dimmed">
                {[e.fromDate ? fmtAd(e.fromDate) : null, e.toDate ? fmtAd(e.toDate) : null].filter(Boolean).join(' to ')}
                {e.reasonForLeaving ? ` · Left: ${e.reasonForLeaving}` : ''}
              </Text>
            </div>
          ))}
        </Stack>
      ) : (
        <Text size="sm" c="dimmed">
          No previous employment recorded.
        </Text>
      );
    case 'skills':
      return p.skills.skills.length || p.skills.languages.length ? (
        <Group gap={6}>
          {p.skills.skills.map((s) => (
            <Badge key={`s${s}`} variant="light" color="brand">
              {s}
            </Badge>
          ))}
          {p.skills.languages.map((s) => (
            <Badge key={`l${s}`} variant="outline" color="gray">
              {s}
            </Badge>
          ))}
        </Group>
      ) : (
        <Text size="sm" c="dimmed">
          No skills or languages listed.
        </Text>
      );
    default:
      return null;
  }
}

/** The value an edit form starts from: secret numbers start empty (blank keeps the stored one). */
function initialValue(section, p) {
  const v = structuredClone(p[section]);
  for (const k of Object.keys(v)) if (SECRET.has(k)) v[k] = '';
  return v;
}

/** Strip what the server should not receive: empty secrets (unchanged), empty strings become null in flat sections. */
function toPayload(section, v) {
  const out = {};
  const listSections = ['emergency', 'family', 'education', 'experience', 'skills'];
  if (listSections.includes(section)) {
    const clean = (o) => Object.fromEntries(Object.entries(o).map(([k, x]) => [k, x === '' ? null : x]));
    for (const [k, x] of Object.entries(v)) out[k] = Array.isArray(x) ? x.map((i) => (i && typeof i === 'object' ? clean(i) : i)) : x;
    return out;
  }
  for (const [k, x] of Object.entries(v)) {
    if (SECRET.has(k)) {
      if (typeof x === 'string' && x.trim()) out[k] = x.trim();
      continue;
    }
    if (x && typeof x === 'object' && !Array.isArray(x)) {
      const a = Object.fromEntries(Object.entries(x).filter(([, y]) => y !== '' && y !== null && y !== undefined));
      out[k] = Object.keys(a).length ? a : null;
      continue;
    }
    out[k] = x === '' ? null : x;
  }
  return out;
}

function AddressFields({ value, onChange, title }) {
  const a = value ?? {};
  const set = (k, x) => onChange({ ...a, [k]: x });
  return (
    <Stack gap="xs">
      <Text size="sm" fw={600}>
        {title}
      </Text>
      <SimpleGrid cols={2} spacing="xs">
        <Select label="Province" clearable data={PROVINCES} value={a.province ?? null} onChange={(x) => set('province', x)} />
        <TextInput label="District" value={a.district ?? ''} onChange={(e) => set('district', e.currentTarget.value)} />
        <TextInput label="Municipality" value={a.municipality ?? ''} onChange={(e) => set('municipality', e.currentTarget.value)} />
        <NumberInput label="Ward" min={1} max={40} value={a.ward ?? ''} onChange={(x) => set('ward', x === '' ? null : Number(x))} />
      </SimpleGrid>
      <TextInput label="Street or tole" value={a.street ?? ''} onChange={(e) => set('street', e.currentTarget.value)} />
    </Stack>
  );
}

/** A repeated list editor: one card per entry, add and remove. */
function ListEditor({ items, onChange, blank, render, addLabel, max }) {
  return (
    <Stack gap="sm">
      {items.map((it, i) => (
        <Paper key={i} withBorder p="sm">
          <Group justify="flex-end" mb={-8}>
            <ActionIcon
              variant="subtle"
              color="red"
              size="sm"
              onClick={() => onChange(items.filter((_, j) => j !== i))}
              aria-label="Remove"
            >
              <IconTrash size={14} stroke={1.6} />
            </ActionIcon>
          </Group>
          <Stack gap="xs">{render(it, (k, v) => onChange(items.map((x, j) => (j === i ? { ...x, [k]: v } : x))))}</Stack>
        </Paper>
      ))}
      {items.length < max ? (
        <Button variant="default" size="xs" leftSection={<IconPlus {...ICON} />} onClick={() => onChange([...items, { ...blank }])}>
          {addLabel}
        </Button>
      ) : null}
    </Stack>
  );
}

function SectionForm({ section, value, onChange, masked }) {
  const set = (k, x) => onChange({ ...value, [k]: x });
  const s = (k) => value[k] ?? '';
  const keep = (k, current) => (current ? `Leave blank to keep ${current}` : undefined);
  switch (section) {
    case 'personal':
      return (
        <Stack gap="sm">
          <Select label="Gender" clearable data={GENDER} value={s('gender') || null} onChange={(x) => set('gender', x)} />
          <DateField label="Date of birth" value={s('dob') || null} onChange={(x) => set('dob', x)} />
          <Select
            label="Marital status"
            clearable
            data={MARITAL}
            value={s('maritalStatus') || null}
            onChange={(x) => set('maritalStatus', x)}
            description="Used for the income tax slab (married couples file jointly)."
          />
          <Select label="Blood group" clearable data={BLOOD} value={s('bloodGroup') || null} onChange={(x) => set('bloodGroup', x)} />
          <TextInput label="Nationality" value={s('nationality')} onChange={(e) => set('nationality', e.currentTarget.value)} />
        </Stack>
      );
    case 'contact':
      return (
        <Stack gap="sm">
          <TextInput
            label="Personal email"
            type="email"
            value={s('personalEmail')}
            onChange={(e) => set('personalEmail', e.currentTarget.value)}
          />
          <SimpleGrid cols={2} spacing="xs">
            <TextInput
              label="Personal phone"
              value={s('personalPhone')}
              onChange={(e) => set('personalPhone', e.currentTarget.value)}
              placeholder="98XXXXXXXX"
            />
            <TextInput label="Work phone" value={s('workPhone')} onChange={(e) => set('workPhone', e.currentTarget.value)} />
          </SimpleGrid>
          <Divider />
          <AddressFields title="Permanent address" value={value.permanentAddress} onChange={(a) => set('permanentAddress', a)} />
          <Divider />
          <AddressFields title="Temporary (current) address" value={value.temporaryAddress} onChange={(a) => set('temporaryAddress', a)} />
        </Stack>
      );
    case 'bank':
      return (
        <Stack gap="sm">
          <TextInput
            label="Bank"
            placeholder="e.g. NABIL"
            value={s('bankCode')}
            onChange={(e) => set('bankCode', e.currentTarget.value.toUpperCase())}
          />
          <TextInput label="Branch" value={s('bankBranch')} onChange={(e) => set('bankBranch', e.currentTarget.value)} />
          <TextInput
            label="Account number"
            value={s('bankAccount')}
            onChange={(e) => set('bankAccount', e.currentTarget.value.replace(/\s/g, ''))}
            {...(keep('bankAccount', masked.bank.bankAccount) ? { description: keep('bankAccount', masked.bank.bankAccount) } : {})}
            autoComplete="off"
          />
        </Stack>
      );
    case 'ids':
      return (
        <Stack gap="sm">
          <TextInput
            label="PAN"
            maxLength={9}
            value={s('pan')}
            onChange={(e) => set('pan', e.currentTarget.value.replace(/\D/g, ''))}
            {...(masked.ids.pan ? { description: keep('pan', masked.ids.pan) } : {})}
            autoComplete="off"
          />
          <TextInput
            label="Citizenship number"
            value={s('citizenshipNo')}
            onChange={(e) => set('citizenshipNo', e.currentTarget.value)}
            {...(masked.ids.citizenshipNo ? { description: keep('citizenshipNo', masked.ids.citizenshipNo) } : {})}
            autoComplete="off"
          />
          <SimpleGrid cols={2} spacing="xs">
            <TextInput
              label="Issue district"
              value={s('citizenshipDistrict')}
              onChange={(e) => set('citizenshipDistrict', e.currentTarget.value)}
            />
            <DateField label="Issue date" value={s('citizenshipIssuedOn') || null} onChange={(x) => set('citizenshipIssuedOn', x)} />
          </SimpleGrid>
          <SimpleGrid cols={2} spacing="xs">
            <TextInput
              label="Passport number"
              value={s('passportNo')}
              onChange={(e) => set('passportNo', e.currentTarget.value.toUpperCase())}
              {...(masked.ids.passportNo ? { description: keep('passportNo', masked.ids.passportNo) } : {})}
              autoComplete="off"
            />
            <DateField label="Passport expiry" value={s('passportExpiry') || null} onChange={(x) => set('passportExpiry', x)} />
          </SimpleGrid>
          <SimpleGrid cols={2} spacing="xs">
            <TextInput
              label="Driving licence number"
              value={s('licenceNo')}
              onChange={(e) => set('licenceNo', e.currentTarget.value)}
              {...(masked.ids.licenceNo ? { description: keep('licenceNo', masked.ids.licenceNo) } : {})}
              autoComplete="off"
            />
            <DateField label="Licence expiry" value={s('licenceExpiry') || null} onChange={(x) => set('licenceExpiry', x)} />
          </SimpleGrid>
          <SimpleGrid cols={3} spacing="xs">
            <TextInput label="SSF ID" value={s('ssfId')} onChange={(e) => set('ssfId', e.currentTarget.value)} />
            <TextInput label="CIT ID" value={s('citId')} onChange={(e) => set('citId', e.currentTarget.value)} />
            <TextInput label="PF number" value={s('pfId')} onChange={(e) => set('pfId', e.currentTarget.value)} />
          </SimpleGrid>
        </Stack>
      );
    case 'emergency':
      return (
        <ListEditor
          items={value.contacts ?? []}
          onChange={(x) => set('contacts', x)}
          max={5}
          addLabel="Add a contact"
          blank={{ name: '', relation: '', phone: '', altPhone: '', address: '' }}
          render={(c, up) => (
            <>
              <SimpleGrid cols={2} spacing="xs">
                <TextInput label="Name" required value={c.name} onChange={(e) => up('name', e.currentTarget.value)} />
                <TextInput label="Relation" required value={c.relation} onChange={(e) => up('relation', e.currentTarget.value)} />
                <TextInput label="Phone" required value={c.phone} onChange={(e) => up('phone', e.currentTarget.value)} />
                <TextInput label="Other phone" value={c.altPhone ?? ''} onChange={(e) => up('altPhone', e.currentTarget.value)} />
              </SimpleGrid>
              <TextInput label="Address" value={c.address ?? ''} onChange={(e) => up('address', e.currentTarget.value)} />
            </>
          )}
        />
      );
    case 'family': {
      const members = value.members ?? [];
      const total = members.filter((m) => m.isNominee).reduce((acc, m) => acc + (Number(m.nomineeShare) || 0), 0);
      return (
        <Stack gap="sm">
          <ListEditor
            items={members}
            onChange={(x) => set('members', x)}
            max={20}
            addLabel="Add a family member"
            blank={{
              name: '',
              relation: 'spouse',
              dob: null,
              occupation: '',
              phone: '',
              isDependent: false,
              isNominee: false,
              nomineeShare: null,
            }}
            render={(m, up) => (
              <>
                <SimpleGrid cols={2} spacing="xs">
                  <TextInput label="Name" required value={m.name} onChange={(e) => up('name', e.currentTarget.value)} />
                  <Select
                    label="Relation"
                    data={RELATIONS}
                    value={m.relation}
                    onChange={(x) => up('relation', x ?? 'other')}
                    allowDeselect={false}
                  />
                  <DateField label="Date of birth" value={m.dob ?? null} onChange={(x) => up('dob', x)} />
                  <TextInput label="Occupation" value={m.occupation ?? ''} onChange={(e) => up('occupation', e.currentTarget.value)} />
                </SimpleGrid>
                <Group gap="lg" align="flex-end">
                  <Checkbox label="Dependent" checked={m.isDependent} onChange={(e) => up('isDependent', e.currentTarget.checked)} />
                  <Checkbox
                    label="Nominee"
                    checked={m.isNominee}
                    onChange={(e) => {
                      const on = e.currentTarget.checked;
                      up('isNominee', on);
                      if (!on) up('nomineeShare', null);
                    }}
                  />
                  {m.isNominee ? (
                    <NumberInput
                      label="Share %"
                      w={110}
                      min={1}
                      max={100}
                      value={m.nomineeShare ?? ''}
                      onChange={(x) => up('nomineeShare', x === '' ? null : Number(x))}
                    />
                  ) : null}
                </Group>
              </>
            )}
          />
          {members.some((m) => m.isNominee) ? (
            <Text size="xs" c={Math.abs(total - 100) < 0.01 ? 'dimmed' : 'red'}>
              Nominee shares add up to {total}%{Math.abs(total - 100) < 0.01 ? '.' : '; they must total 100%.'}
            </Text>
          ) : null}
        </Stack>
      );
    }
    case 'education':
      return (
        <ListEditor
          items={value.items ?? []}
          onChange={(x) => set('items', x)}
          max={15}
          addLabel="Add a qualification"
          blank={{ level: 'bachelor', degree: '', field: '', institution: '', board: '', startYear: null, endYear: null, result: '' }}
          render={(e, up) => (
            <SimpleGrid cols={2} spacing="xs">
              <Select label="Level" data={LEVELS} value={e.level} onChange={(x) => up('level', x ?? 'other')} allowDeselect={false} />
              <TextInput
                label="Degree"
                placeholder="e.g. BE Computer"
                value={e.degree ?? ''}
                onChange={(ev) => up('degree', ev.currentTarget.value)}
              />
              <TextInput label="Institution" required value={e.institution} onChange={(ev) => up('institution', ev.currentTarget.value)} />
              <TextInput label="Board or university" value={e.board ?? ''} onChange={(ev) => up('board', ev.currentTarget.value)} />
              <NumberInput
                label="Start year"
                min={1950}
                max={2200}
                value={e.startYear ?? ''}
                onChange={(x) => up('startYear', x === '' ? null : Number(x))}
              />
              <NumberInput
                label="End year"
                min={1950}
                max={2200}
                value={e.endYear ?? ''}
                onChange={(x) => up('endYear', x === '' ? null : Number(x))}
              />
              <TextInput label="Field" value={e.field ?? ''} onChange={(ev) => up('field', ev.currentTarget.value)} />
              <TextInput
                label="Result"
                placeholder="GPA or division"
                value={e.result ?? ''}
                onChange={(ev) => up('result', ev.currentTarget.value)}
              />
            </SimpleGrid>
          )}
        />
      );
    case 'experience':
      return (
        <ListEditor
          items={value.items ?? []}
          onChange={(x) => set('items', x)}
          max={20}
          addLabel="Add a previous job"
          blank={{ organisation: '', designation: '', fromDate: null, toDate: null, responsibilities: '', reasonForLeaving: '' }}
          render={(e, up) => (
            <>
              <SimpleGrid cols={2} spacing="xs">
                <TextInput
                  label="Organisation"
                  required
                  value={e.organisation}
                  onChange={(ev) => up('organisation', ev.currentTarget.value)}
                />
                <TextInput label="Designation" value={e.designation ?? ''} onChange={(ev) => up('designation', ev.currentTarget.value)} />
                <DateField label="From" value={e.fromDate ?? null} onChange={(x) => up('fromDate', x)} />
                <DateField label="To" value={e.toDate ?? null} onChange={(x) => up('toDate', x)} />
              </SimpleGrid>
              <Textarea
                label="Responsibilities"
                autosize
                minRows={2}
                value={e.responsibilities ?? ''}
                onChange={(ev) => up('responsibilities', ev.currentTarget.value)}
              />
              <TextInput
                label="Reason for leaving"
                value={e.reasonForLeaving ?? ''}
                onChange={(ev) => up('reasonForLeaving', ev.currentTarget.value)}
              />
            </>
          )}
        />
      );
    case 'skills':
      return (
        <Stack gap="sm">
          <TagsInput
            label="Skills"
            description="Press Enter after each one."
            value={value.skills ?? []}
            onChange={(x) => set('skills', x)}
            maxTags={30}
          />
          <TagsInput
            label="Languages"
            value={value.languages ?? []}
            onChange={(x) => set('languages', x)}
            maxTags={15}
            data={['Nepali', 'English', 'Hindi', 'Maithili', 'Newari', 'Bhojpuri', 'Tamang']}
          />
        </Stack>
      );
    default:
      return null;
  }
}

/**
 * The full employee profile, section by section. HR edits directly (bank and ID changes may need a second HR approval);
 * the employee asks for a change, optionally with proof, and HR approves it.
 */
export function ProfileSections({ employeeId, mode, onRequested }) {
  const [reveal, setReveal] = useState(false);
  const q = useQuery(() => people.profile(employeeId, reveal), [employeeId, reveal]);
  const [editing, setEditing] = useState(null);
  const [reason, setReason] = useState('');
  const [proof, setProof] = useState(null);
  const [busy, setBusy] = useState(false);
  const p = q.data?.profile;
  const pending = new Set((q.data?.pending ?? []).map((x) => x.section));

  if (q.error)
    return (
      <EmptyState title="We could not load the profile" hint={q.error} action={{ label: 'Try again', onClick: () => void q.reload() }} />
    );
  if (!p)
    return (
      <Stack gap="md">
        {ORDER.slice(0, 4).map((s) => (
          <Skeleton key={s} h={110} radius="md" />
        ))}
      </Stack>
    );

  const open = (section) => {
    setEditing({ section, value: initialValue(section, p) });
    setReason('');
    setProof(null);
  };
  const submit = async () => {
    if (!editing) return;
    setBusy(true);
    try {
      const value = toPayload(editing.section, editing.value);
      if (mode === 'hr') {
        const r = await people.saveSection(employeeId, editing.section, value);
        if (r.pending)
          toast.info(
            'Saved for approval. A different HR person must approve this change before it applies.',
            'Waiting for a second approval',
          );
        else
          toast.ok(r.changes?.length ? `${r.changes.length} ${r.changes.length === 1 ? 'change' : 'changes'} saved.` : 'Nothing changed.');
      } else {
        await people.requestChange({
          section: editing.section,
          value,
          ...(reason.trim() ? { reason: reason.trim() } : {}),
          ...(proof ? { proofFileId: proof.id } : {}),
        });
        toast.ok('HR will review your change. You will be notified when it is decided.', 'Change requested');
        onRequested?.();
      }
      setEditing(null);
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };

  return (
    <Stack gap="md">
      {mode === 'hr' ? (
        <Group justify="space-between" wrap="wrap" gap="sm">
          <Group gap="xs" wrap="nowrap">
            <IconLock size={16} stroke={1.6} color="var(--hj-muted)" />
            <Text size="sm" c="dimmed">
              PAN, bank and ID numbers are encrypted and shown masked. Revealing them is logged.
            </Text>
          </Group>
          {!reveal ? (
            <Button size="xs" variant="default" leftSection={<IconEye {...ICON} />} onClick={() => setReveal(true)}>
              Reveal numbers
            </Button>
          ) : (
            <Button size="xs" variant="subtle" onClick={() => setReveal(false)}>
              Hide numbers
            </Button>
          )}
        </Group>
      ) : null}
      <SimpleGrid cols={{ base: 1, lg: 2 }} spacing="md">
        {ORDER.map((section) => (
          <Paper key={section} withBorder p="lg">
            <Group justify="space-between" mb="md" wrap="nowrap">
              <Group gap="xs">
                <Text fw={600}>{SECTION_LABEL[section]}</Text>
                {pending.has(section) ? (
                  <Badge size="sm" variant="light" color="yellow">
                    Change waiting for approval
                  </Badge>
                ) : null}
              </Group>
              <Button
                size="compact-sm"
                variant="subtle"
                disabled={mode === 'self' && pending.has(section)}
                leftSection={mode === 'hr' ? <IconPencil size={14} stroke={1.6} /> : <IconSend size={14} stroke={1.6} />}
                onClick={() => open(section)}
              >
                {mode === 'hr' ? 'Edit' : 'Request a change'}
              </Button>
            </Group>
            <SectionView section={section} p={p} />
          </Paper>
        ))}
      </SimpleGrid>

      <Drawer
        opened={!!editing}
        onClose={() => setEditing(null)}
        position="right"
        size="lg"
        title={editing ? `${mode === 'hr' ? 'Edit' : 'Request a change:'} ${SECTION_LABEL[editing.section].toLowerCase()}` : ''}
      >
        {editing ? (
          <Stack gap="md">
            {mode === 'self' ? (
              <Alert color="blue" variant="light">
                HR reviews every change before it is saved. Attach proof (for example a new citizenship or bank letter) to speed things up.
              </Alert>
            ) : null}
            <SectionForm
              section={editing.section}
              value={editing.value}
              onChange={(value) => setEditing({ ...editing, value })}
              masked={p}
            />
            {mode === 'self' ? (
              <>
                <Divider />
                <Textarea
                  label="Note for HR"
                  placeholder="Why is this changing?"
                  autosize
                  minRows={2}
                  maxLength={500}
                  value={reason}
                  onChange={(e) => setReason(e.currentTarget.value)}
                />
                <FileUpload purpose="profile_change" value={proof} onChange={setProof} label="Attach proof (PDF or photo)" />
              </>
            ) : null}
            <Group justify="flex-end" mt="sm">
              <Button variant="default" onClick={() => setEditing(null)}>
                Cancel
              </Button>
              <Button onClick={submit} loading={busy}>
                {mode === 'hr' ? 'Save' : 'Send to HR'}
              </Button>
            </Group>
          </Stack>
        ) : null}
      </Drawer>
    </Stack>
  );
}
