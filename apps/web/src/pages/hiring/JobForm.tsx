import { ActionIcon, Button, Drawer, Group, NumberInput, Paper, Select, SimpleGrid, Stack, Switch, TagsInput, Text, Textarea, TextInput } from '@mantine/core';
import { IconPlus, IconTrash } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { hiring, type Job, type JobQuestion, type Requisition } from '../../api/hiring';
import { DateField } from '../../components/BsDatePicker';
import { EmployeePicker } from '../../components/EmployeePicker';
import { SectionTitle } from '../../components/PageHeader';
import { todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { ICON, useLookups } from './shared';

const DEFAULT_STAGES = ['Applied', 'Screening', 'Interview', 'Offer', 'Hired'];

interface Form {
  requisitionId: string | null; title: string; category: string; location: string; workMode: string; employmentType: string; openings: number;
  experienceMin: number | ''; experienceMax: number | ''; salaryMin: number | ''; salaryMax: number | ''; showSalary: boolean; deadline: string | null;
  orgUnitId: string | null; branchId: string | null; hiringManagerId: string | null; description: string; requirements: string; stages: string[];
  questions: JobQuestion[];
}

type Rule = 'none' | 'knockout' | 'preferred';

function fromJob(j: Job): Form {
  const n = (v: string | null) => (v === null ? '' as const : Number(v));
  return {
    requisitionId: j.requisition_id, title: j.title, category: j.category ?? '', location: j.location ?? '', workMode: j.work_mode, employmentType: j.employment_type,
    openings: j.openings, experienceMin: n(j.experience_min), experienceMax: n(j.experience_max), salaryMin: n(j.salary_min), salaryMax: n(j.salary_max),
    showSalary: j.show_salary, deadline: j.deadline, orgUnitId: j.org_unit_id, branchId: j.branch_id, hiringManagerId: j.hiring_manager_id,
    description: j.description, requirements: j.requirements, stages: j.stages, questions: j.questions,
  };
}

function fromRequisition(r?: Requisition | null): Form {
  return {
    requisitionId: r?.id ?? null, title: r?.title ?? '', category: '', location: '', workMode: 'onsite', employmentType: 'full_time', openings: r ? r.headcount - r.filled : 1,
    experienceMin: '', experienceMax: '', salaryMin: r?.salary_min ? Number(r.salary_min) : '', salaryMax: r?.salary_max ? Number(r.salary_max) : '', showSalary: false,
    deadline: null, orgUnitId: r?.org_unit_id ?? null, branchId: r?.branch_id ?? null, hiringManagerId: null, description: '', requirements: '', stages: DEFAULT_STAGES, questions: [],
  };
}

/** Question ids stay fixed once used, so answers already given keep pointing at the right question. */
function nextQuestionId(qs: JobQuestion[]): string {
  const used = qs.map((q) => Number(q.id.slice(1))).filter(Number.isFinite);
  return `q${(used.length ? Math.max(...used) : 0) + 1}`;
}

function ruleOf(q: JobQuestion): Rule { return q.knockout ? 'knockout' : q.preferred ? 'preferred' : 'none'; }

/** Create or edit a job opening: what the careers page shows, the hiring stages, and the screening questions. */
export function JobForm({ opened, job, requisition, onClose, onSaved }: { opened: boolean; job?: Job | null; requisition?: Requisition | null; onClose: () => void; onSaved: (id: string) => void }) {
  const lookups = useLookups();
  const [f, setF] = useState<Form>(fromRequisition(requisition));
  const [busy, setBusy] = useState(false);
  useEffect(() => { if (opened) setF(job ? fromJob(job) : fromRequisition(requisition)); }, [opened, job, requisition]);
  const set = <K extends keyof Form>(k: K, v: Form[K]) => setF((x) => ({ ...x, [k]: v }));
  const setQ = (i: number, q: Partial<JobQuestion>) => setF((x) => ({ ...x, questions: x.questions.map((o, j) => (j === i ? { ...o, ...q } : o)) }));

  const setRule = (i: number, rule: Rule) => {
    const q = f.questions[i]!;
    const value = q.kind === 'number' ? { min: 1 } : { equals: 'yes' as const };
    const { knockout: _k, preferred: _p, ...rest } = q;
    setF((x) => ({ ...x, questions: x.questions.map((o, j) => (j === i ? (rule === 'none' ? rest : { ...rest, [rule]: value }) : o)) }));
  };

  const save = async () => {
    setBusy(true);
    try {
      const num = (v: number | '') => (v === '' ? null : v);
      const money = (v: number | '') => (v === '' ? null : String(v));
      const body: Record<string, unknown> = {
        title: f.title, category: f.category || null, location: f.location || null, workMode: f.workMode, employmentType: f.employmentType, openings: f.openings,
        experienceMin: num(f.experienceMin), experienceMax: num(f.experienceMax), salaryMin: money(f.salaryMin), salaryMax: money(f.salaryMax), showSalary: f.showSalary,
        deadline: f.deadline, orgUnitId: f.orgUnitId, branchId: f.branchId, hiringManagerId: f.hiringManagerId, description: f.description, requirements: f.requirements,
        stages: f.stages, questions: f.questions,
      };
      if (job) { await hiring.updateJob(job.id, body); onSaved(job.id); }
      else { const r = await hiring.createJob({ ...body, requisitionId: f.requisitionId }); onSaved(r.id); }
      toast.ok(job ? 'Job saved.' : 'Job created as a draft. Publish it when it is ready.');
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };

  const stagesValid = f.stages.length >= 2 && f.stages[0] === 'Applied' && f.stages[f.stages.length - 1] === 'Hired';

  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="xl" title={<Text fw={600}>{job ? `Edit ${job.title}` : 'New job'}</Text>}>
      <Stack gap="lg">
        <div>
          <SectionTitle>The role</SectionTitle>
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <TextInput label="Job title" value={f.title} onChange={(e) => set('title', e.currentTarget.value)} required maxLength={120} />
            <TextInput label="Category" description="Shown as a filter on the careers page" placeholder="Engineering" value={f.category} onChange={(e) => set('category', e.currentTarget.value)} maxLength={60} />
            <Select label="Department" clearable searchable value={f.orgUnitId} onChange={(v) => set('orgUnitId', v)} data={lookups.orgUnits.map((u) => ({ value: u.id, label: u.name_en }))} />
            <Select label="Branch" clearable value={f.branchId} onChange={(v) => set('branchId', v)} data={lookups.branches.map((b) => ({ value: b.id, label: b.name_en }))} />
            <EmployeePicker label="Hiring manager" value={f.hiringManagerId} onChange={(v) => set('hiringManagerId', v)} description="Sees the candidates and is told about new applications" />
            <TextInput label="Location" placeholder="Kathmandu" value={f.location} onChange={(e) => set('location', e.currentTarget.value)} maxLength={120} />
            <Select label="Work mode" allowDeselect={false} value={f.workMode} onChange={(v) => v && set('workMode', v)} data={[{ value: 'onsite', label: 'On site' }, { value: 'hybrid', label: 'Hybrid' }, { value: 'remote', label: 'Remote' }]} />
            <Select label="Employment type" allowDeselect={false} value={f.employmentType} onChange={(v) => v && set('employmentType', v)}
              data={[{ value: 'full_time', label: 'Full-time' }, { value: 'part_time', label: 'Part-time' }, { value: 'contract', label: 'Contract' }, { value: 'internship', label: 'Internship' }, { value: 'temporary', label: 'Temporary' }]} />
            <NumberInput label="Openings" min={1} max={500} value={f.openings} onChange={(v) => set('openings', Number(v) || 1)} />
            <DateField label="Apply by" value={f.deadline} onChange={(v) => set('deadline', v)} minDate={todayNpt()} />
          </SimpleGrid>
        </div>
        <div>
          <SectionTitle hint="Experience is used in the screening score. Salary is only shown publicly when you switch it on.">Experience and pay</SectionTitle>
          <SimpleGrid cols={{ base: 2, sm: 4 }}>
            <NumberInput label="Min. years" min={0} max={60} decimalScale={1} value={f.experienceMin} onChange={(v) => set('experienceMin', v === '' ? '' : Number(v))} />
            <NumberInput label="Max. years" min={0} max={60} decimalScale={1} value={f.experienceMax} onChange={(v) => set('experienceMax', v === '' ? '' : Number(v))} />
            <NumberInput label="Salary from (NPR)" min={0} thousandSeparator="," value={f.salaryMin} onChange={(v) => set('salaryMin', v === '' ? '' : Number(v))} />
            <NumberInput label="Salary to (NPR)" min={0} thousandSeparator="," value={f.salaryMax} onChange={(v) => set('salaryMax', v === '' ? '' : Number(v))} />
          </SimpleGrid>
          <Switch mt="sm" label="Show the salary range on the careers page" checked={f.showSalary} onChange={(e) => set('showSalary', e.currentTarget.checked)} />
        </div>
        <div>
          <SectionTitle>Description</SectionTitle>
          <Stack gap="sm">
            <Textarea label="About the role" description="Plain text. Start a line with - for a bullet point." autosize minRows={5} maxRows={16} value={f.description} onChange={(e) => set('description', e.currentTarget.value)} maxLength={10000} />
            <Textarea label="What you need" autosize minRows={3} maxRows={12} value={f.requirements} onChange={(e) => set('requirements', e.currentTarget.value)} maxLength={10000} />
          </Stack>
        </div>
        <div>
          <SectionTitle hint='Candidates move through these columns. The first must be "Applied" and the last "Hired".'>Hiring stages</SectionTitle>
          <TagsInput value={f.stages} onChange={(v) => set('stages', v)} maxTags={10} error={stagesValid ? undefined : 'Start with "Applied" and end with "Hired".'} />
        </div>
        <div>
          <SectionTitle hint="A must-have flags anyone who answers it wrongly. A preferred answer adds 20 points to the screening score."
            actions={<Button size="xs" variant="light" leftSection={<IconPlus size={14} />} disabled={f.questions.length >= 15}
              onClick={() => set('questions', [...f.questions, { id: nextQuestionId(f.questions), kind: 'yes_no', label: '', required: true }])}>Add question</Button>}>
            Screening questions
          </SectionTitle>
          <Stack gap="sm">
            {f.questions.length === 0 ? <Text size="sm" c="dimmed">No questions. Applicants send their details and CV only.</Text> : null}
            {f.questions.map((q, i) => {
              const rule = ruleOf(q); const rr = q.knockout ?? q.preferred;
              return (
                <Paper key={i} withBorder p="sm" radius="md">
                  <Group align="flex-end" gap="sm" wrap="wrap">
                    <TextInput style={{ flex: '1 1 260px' }} label={`Question ${i + 1}`} value={q.label} onChange={(e) => setQ(i, { label: e.currentTarget.value })} maxLength={200} />
                    <Select w={130} label="Answer" allowDeselect={false} value={q.kind} data={[{ value: 'yes_no', label: 'Yes or no' }, { value: 'number', label: 'Number' }, { value: 'text', label: 'Text' }]}
                      onChange={(v) => { if (!v) return; const { knockout: _k, preferred: _p, ...rest } = q; setF((x) => ({ ...x, questions: x.questions.map((o, j) => (j === i ? { ...rest, kind: v as JobQuestion['kind'] } : o)) })); }} />
                    <Select w={150} label="Rule" allowDeselect={false} value={rule} disabled={q.kind === 'text'} onChange={(v) => v && setRule(i, v as Rule)}
                      data={[{ value: 'none', label: 'Just ask' }, { value: 'knockout', label: 'Must-have' }, { value: 'preferred', label: 'Preferred' }]} />
                    {rr && q.kind === 'yes_no' ? <Select w={100} label="Answer must be" allowDeselect={false} value={rr.equals ?? 'yes'} data={[{ value: 'yes', label: 'Yes' }, { value: 'no', label: 'No' }]}
                      onChange={(v) => v && setQ(i, { [rule]: { equals: v } } as Partial<JobQuestion>)} /> : null}
                    {rr && q.kind === 'number' ? <NumberInput w={110} label="At least" min={0} value={rr.min ?? 0} onChange={(v) => setQ(i, { [rule]: { min: Number(v) || 0 } } as Partial<JobQuestion>)} /> : null}
                    <Switch label="Required" checked={q.required} onChange={(e) => setQ(i, { required: e.currentTarget.checked })} mb={8} />
                    <ActionIcon variant="subtle" color="red" aria-label="Remove question" mb={4} onClick={() => set('questions', f.questions.filter((_, j) => j !== i))}><IconTrash {...ICON} /></ActionIcon>
                  </Group>
                </Paper>
              );
            })}
          </Stack>
        </div>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>Cancel</Button>
          <Button onClick={save} loading={busy} disabled={f.title.trim().length < 2 || !stagesValid || f.questions.some((q) => q.label.trim().length < 3)}>{job ? 'Save job' : 'Create job'}</Button>
        </Group>
      </Stack>
    </Drawer>
  );
}
