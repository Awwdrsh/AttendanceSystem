import { Avatar, Button, Drawer, Group, Paper, SimpleGrid, Stack, Table, Tabs, Text } from '@mantine/core';
import { IconEye, IconFingerprint, IconLock, IconPencil, IconReceipt, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';
import { Link, useParams } from 'react-router-dom';
import { biometrics as bioApi, employees as empApi, leave as leaveApi, payroll as payApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { DataTable } from '../../components/DataTable';
import { EmployeeForm } from '../../components/EmployeeForm';
import { EmptyState } from '../../components/EmptyState';
import { Field } from '../../components/Kpi';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { StatusBadge } from '../../components/StatusBadge';
import { bsMonthLabel, fmtAd, fmtMoney, fmtTime, NPT_OFFSET_MIN } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { LeaveApplyForm } from '../employee/LeavePage';
import { EmployeeDocumentsTab } from '../documents/EmployeeDocumentsTab';
import { LifecycleSection } from '../lifecycle/LifecycleSection';
import { EmployeeRelationsSection } from '../../components/relations/EmployeeRelationsSection';
import { CareerTimeline } from '../../components/people/CareerTimeline';
import { EmployeeProfilePanel } from '../../components/people/EmployeeProfilePanel';
import { FieldHistory } from '../../components/people/FieldHistory';

const DAY_NAMES = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];
const EMPLOYMENT = { regular: 'Regular', work_based: 'Work-based', time_based: 'Time-based', casual: 'Casual', part_time: 'Part-time' };
const REGIME = { ssf: 'SSF', pf: 'PF and gratuity', none: 'None' };

/** Calendar date in Nepal time for an ISO instant. */
function nptDate(iso) {
  const ms = Date.parse(iso);
  return Number.isNaN(ms) ? iso : new Date(ms + NPT_OFFSET_MIN * 60_000).toISOString().slice(0, 10);
}

/** Postgres daterange "[2026-01-01,2026-06-30)" becomes "1 Jan 2026 to 30 Jun 2026". */
function fmtRange(v) {
  const m = /^[[(]([\d-]*),([\d-]*)[\])]$/.exec(String(v ?? ''));
  if (!m) return String(v ?? '—');
  return `${m[1] ? fmtAd(m[1]) : 'Start'} to ${m[2] ? fmtAd(m[2]) : 'present'}`;
}

function humanize(v) {
  if (v === null || v === undefined || v === '') return '—';
  const s = String(v).replace(/_/g, ' ');
  return s.charAt(0).toUpperCase() + s.slice(1);
}

export default function EmployeeDetailPage() {
  const { id = '' } = useParams();
  const { hasRole } = useAuth();
  const emp = useQuery(() => empApi.get(id), [id]);
  const balances = useQuery(() => leaveApi.balances(id), [id]);
  const assignments = useQuery(() => empApi.assignments(id), [id]);
  const audit = useQuery(() => empApi.audit(id), [id]);
  const templates = useQuery(() => bioApi.templates(id), [id]);
  const consents = useQuery(() => bioApi.consents(id), [id]);
  const payslips = useQuery(() => payApi.payslips({ employeeId: id }), [id], { enabled: hasRole('hr') });
  const [sensitive, setSensitive] = useState(null);
  const [busy, setBusy] = useState(false);
  const [shred, setShred] = useState(false);
  const [editing, setEditing] = useState(false);
  const e = emp.data?.employee;
  const back = { to: '/employees', label: 'Employees' };
  if (emp.error)
    return (
      <>
        <PageHeader title="Employee not found" back={back} />
        <EmptyState title="We could not load this employee" hint={emp.error} />
      </>
    );
  if (!e) return null;
  const subtitle = [e.designation, `Code ${e.emp_code}`, e.join_date ? `Joined ${fmtAd(e.join_date)}` : null].filter(Boolean).join(', ');
  const recordConsent = async (modality) => {
    try {
      await bioApi.consent({ employeeId: id, modality, noticeVersion: '2026-10' });
      toast.ok('Consent recorded.');
      await consents.reload();
    } catch (err) {
      toast.err(err);
    }
  };
  return (
    <Stack gap="md">
      <PageHeader
        back={back}
        title={
          <span style={{ display: 'inline-flex', alignItems: 'center', gap: 12 }}>
            <Avatar component="span" name={e.name_en} size={40} radius="xl" color="brand" variant="light" />
            {e.name_en}
          </span>
        }
        subtitle={subtitle}
        actions={
          <>
            <StatusBadge status={e.status} />
            <Button
              component={Link}
              to={`/me/payslips?employeeId=${id}`}
              variant="default"
              leftSection={<IconReceipt size={16} stroke={1.6} />}
            >
              Payslips
            </Button>
            <Button leftSection={<IconPencil size={16} stroke={1.6} />} onClick={() => setEditing(true)}>
              Edit profile
            </Button>
          </>
        }
      />
      <Tabs defaultValue="overview" keepMounted={false}>
        <Tabs.List>
          <Tabs.Tab value="overview">Overview</Tabs.Tab>
          <Tabs.Tab value="profile">Profile</Tabs.Tab>
          <Tabs.Tab value="career">Career</Tabs.Tab>
          <Tabs.Tab value="leave">Leave</Tabs.Tab>
          <Tabs.Tab value="history">Employment history</Tabs.Tab>
          <Tabs.Tab value="lifecycle">Lifecycle</Tabs.Tab>
          <Tabs.Tab value="relations">Relations</Tabs.Tab>
          <Tabs.Tab value="pay">Pay</Tabs.Tab>
          <Tabs.Tab value="documents">Documents</Tabs.Tab>
          <Tabs.Tab value="biometrics">Biometrics</Tabs.Tab>
          <Tabs.Tab value="changes">Change history</Tabs.Tab>
          <Tabs.Tab value="audit">Audit</Tabs.Tab>
        </Tabs.List>

        <Tabs.Panel value="overview" pt="lg">
          <Stack gap="lg">
            <Paper withBorder p="lg">
              <SimpleGrid cols={{ base: 1, xs: 2, md: 4 }} spacing="lg" verticalSpacing="md">
                <Field label="Email">{e.email ?? '—'}</Field>
                <Field label="Phone">{e.phone ?? '—'}</Field>
                <Field label="Gender">{humanize(e.gender)}</Field>
                <Field label="Date of birth">{fmtAd(e.dob)}</Field>
                <Field label="Join date">{fmtAd(e.join_date)}</Field>
                <Field label="Probation ends">{fmtAd(e.probation_end)}</Field>
                <Field label="Employment type">
                  {e.employment_type ? (EMPLOYMENT[e.employment_type] ?? humanize(e.employment_type)) : '—'}
                </Field>
                <Field label="Grade">{e.grade ?? '—'}</Field>
                <Field label="Weekly off">
                  {e.weekly_off_days.length ? e.weekly_off_days.map((d) => DAY_NAMES[d] ?? d).join(', ') : 'None'}
                </Field>
                <Field label="Overtime">{e.ot_eligible ? 'Eligible' : 'Not eligible'}</Field>
                <Field label="Work from home">{e.work_from_home ? 'Allowed' : 'Office only'}</Field>
                <Field label="Retirement scheme">{REGIME[e.regime] ?? e.regime}</Field>
                <Field label="Exit date">{fmtAd(e.exit_date)}</Field>
              </SimpleGrid>
            </Paper>
            <Paper withBorder p="lg">
              <Group justify="space-between" wrap="wrap" gap="sm">
                <Group gap="sm" wrap="nowrap">
                  <IconLock size={18} stroke={1.6} color="var(--hj-muted)" />
                  <div>
                    <Text fw={600} size="sm">
                      Sensitive fields
                    </Text>
                    <Text size="xs" c="dimmed">
                      PAN and bank account are encrypted. Revealing them is logged.
                    </Text>
                  </div>
                </Group>
                {sensitive ? (
                  <Group gap="xl">
                    <Field label="PAN">{sensitive.pan ?? '—'}</Field>
                    <Field label="Bank account">{sensitive.bankAccount ?? '—'}</Field>
                  </Group>
                ) : (
                  <Button
                    variant="default"
                    leftSection={<IconEye size={16} stroke={1.6} />}
                    onClick={async () => {
                      try {
                        setSensitive(await empApi.sensitive(id));
                      } catch (err) {
                        toast.err(err);
                      }
                    }}
                  >
                    Reveal
                  </Button>
                )}
              </Group>
            </Paper>
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="profile" pt="lg">
          <EmployeeProfilePanel employeeId={id} />
        </Tabs.Panel>
        <Tabs.Panel value="career" pt="lg">
          <CareerTimeline
            employeeId={id}
            canEdit={hasRole('hr')}
            onChanged={() => {
              void emp.reload();
              void assignments.reload();
            }}
          />
        </Tabs.Panel>
        <Tabs.Panel value="changes" pt="lg">
          <FieldHistory employeeId={id} />
        </Tabs.Panel>

        <Tabs.Panel value="leave" pt="lg">
          <Stack gap="lg">
            <div>
              <SectionTitle>Balances</SectionTitle>
              <DataTable
                rows={balances.data?.balances ?? []}
                rowKey={(b) => b.leaveTypeId}
                loading={balances.loading}
                error={balances.error}
                emptyTitle="No leave balances"
                emptyHint="Balances appear once leave types are set up."
                columns={[
                  {
                    key: 'type',
                    header: 'Leave type',
                    render: (b) => (
                      <Text size="sm" fw={500}>
                        {b.nameEn}
                      </Text>
                    ),
                  },
                  {
                    key: 'available',
                    header: 'Available',
                    align: 'right',
                    render: (b) => <span className="num">{b.available.toFixed(1)} days</span>,
                  },
                  {
                    key: 'explanation',
                    header: 'How it adds up',
                    render: (b) => (
                      <Text size="xs" c="dimmed">
                        {b.explanation}
                      </Text>
                    ),
                  },
                ]}
              />
            </div>
            <Paper withBorder p="lg">
              <SectionTitle hint="Files a leave request for this employee.">Apply on their behalf</SectionTitle>
              <LeaveApplyForm
                employeeId={id}
                onDone={() => {
                  void balances.reload();
                }}
              />
            </Paper>
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="history" pt="lg">
          <DataTable
            rows={assignments.data?.assignments ?? []}
            rowKey={(r) => String(r.valid)}
            loading={assignments.loading}
            error={assignments.error}
            emptyTitle="No employment history yet"
            emptyHint="Changes to branch, manager or employment type are recorded here."
            columns={[
              { key: 'valid', header: 'Period', render: (r) => <span className="num">{fmtRange(r.valid)}</span> },
              {
                key: 'employment_type',
                header: 'Type',
                render: (r) => EMPLOYMENT[String(r.employment_type)] ?? humanize(r.employment_type),
              },
              { key: 'branch_id', header: 'Branch', render: (r) => String(r.branch_id ?? '—') },
              { key: 'manager_id', header: 'Manager', render: (r) => String(r.manager_id ?? '—') },
            ]}
          />
        </Tabs.Panel>

        <Tabs.Panel value="lifecycle" pt="lg">
          <LifecycleSection employeeId={id} />
        </Tabs.Panel>
        <Tabs.Panel value="relations" pt="lg">
          <EmployeeRelationsSection employeeId={id} />
        </Tabs.Panel>

        <Tabs.Panel value="pay" pt="lg">
          <Stack gap="md">
            <Group gap="xs">
              <Button component={Link} to={`/payroll/assignments?employeeId=${id}`} variant="default">
                Salary assignments
              </Button>
              <Button component={Link} to={`/payroll/loans?employeeId=${id}`} variant="default">
                Loans
              </Button>
            </Group>
            <DataTable
              rows={payslips.data?.payslips ?? []}
              rowKey={(r) => r.id}
              loading={payslips.loading}
              error={payslips.error}
              emptyTitle="No payslips yet"
              emptyHint="Payslips appear here after a payroll run is calculated."
              columns={[
                {
                  key: 'p',
                  header: 'Month',
                  render: (r) => (
                    <Text size="sm" fw={500}>
                      {r.bs_year && r.bs_month ? bsMonthLabel(r.bs_year, r.bs_month) : '—'}
                      {r.run_type && r.run_type !== 'regular' ? (
                        <Text span size="xs" c="dimmed">
                          {' '}
                          {humanize(r.run_type)}
                        </Text>
                      ) : null}
                    </Text>
                  ),
                },
                { key: 'net', header: 'Net pay', align: 'right', render: (r) => <span className="num">{fmtMoney(r.net)}</span> },
                { key: 'pub', header: 'Status', render: (r) => <StatusBadge status={r.published_at ? 'published' : 'draft'} /> },
                {
                  key: 'open',
                  header: '',
                  align: 'right',
                  render: (r) => (
                    <Button component={Link} to={`/me/payslips/${r.id}`} size="xs" variant="subtle">
                      Open
                    </Button>
                  ),
                },
              ]}
            />
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="documents" pt="lg">
          {id ? <EmployeeDocumentsTab employeeId={id} /> : null}
        </Tabs.Panel>

        <Tabs.Panel value="biometrics" pt="lg">
          <Stack gap="lg">
            <Group gap="xs" wrap="wrap">
              <Button variant="default" leftSection={<IconFingerprint size={16} stroke={1.6} />} onClick={() => recordConsent('face')}>
                Record face consent
              </Button>
              <Button
                variant="default"
                leftSection={<IconFingerprint size={16} stroke={1.6} />}
                onClick={() => recordConsent('fingerprint')}
              >
                Record fingerprint consent
              </Button>
              <Button
                variant="subtle"
                color="red"
                leftSection={<IconTrash size={16} stroke={1.6} />}
                onClick={() => setShred(true)}
                ml="auto"
              >
                Shred all templates
              </Button>
            </Group>
            <div>
              <SectionTitle>Consents</SectionTitle>
              {(consents.data?.consents ?? []).length ? (
                <Paper withBorder style={{ overflow: 'hidden' }}>
                  <Table>
                    <Table.Thead>
                      <Table.Tr>
                        <Table.Th>Modality</Table.Th>
                        <Table.Th>Notice version</Table.Th>
                        <Table.Th>Granted</Table.Th>
                        <Table.Th>Status</Table.Th>
                      </Table.Tr>
                    </Table.Thead>
                    <Table.Tbody>
                      {(consents.data?.consents ?? []).map((c) => (
                        <Table.Tr key={c.id}>
                          <Table.Td>{humanize(c.modality)}</Table.Td>
                          <Table.Td className="num">{c.notice_version}</Table.Td>
                          <Table.Td className="num">{fmtAd(nptDate(c.granted_at))}</Table.Td>
                          <Table.Td>
                            <StatusBadge status={c.revoked_at ? 'cancelled' : 'active'} />
                          </Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                </Paper>
              ) : (
                <EmptyState
                  compact
                  title="No consent recorded"
                  hint="Record consent before enrolling a face or fingerprint."
                  icon={<IconFingerprint size={20} stroke={1.6} />}
                />
              )}
            </div>
            <div>
              <SectionTitle>Templates</SectionTitle>
              <DataTable
                rows={templates.data?.templates ?? []}
                rowKey={(r) => r.id}
                loading={templates.loading}
                error={templates.error}
                emptyTitle="No templates stored"
                emptyHint="Templates appear after the employee enrols on a terminal."
                columns={[
                  { key: 'modality', header: 'Modality', render: (r) => humanize(r.modality) },
                  { key: 'vendor_format', header: 'Format' },
                  { key: 'device_sn', header: 'Device', render: (r) => r.device_sn ?? '—' },
                  { key: 'created_at', header: 'Enrolled', render: (r) => <span className="num">{fmtAd(nptDate(r.created_at))}</span> },
                  {
                    key: 'shredded_at',
                    header: 'Shredded',
                    render: (r) => <span className="num">{r.shredded_at ? fmtAd(nptDate(r.shredded_at)) : '—'}</span>,
                  },
                ]}
              />
            </div>
          </Stack>
        </Tabs.Panel>

        <Tabs.Panel value="audit" pt="lg">
          <DataTable
            rows={audit.data?.events ?? []}
            rowKey={(r) => String(r.id)}
            loading={audit.loading}
            error={audit.error}
            emptyTitle="No changes recorded yet"
            columns={[
              {
                key: 'occurred_at',
                header: 'When',
                render: (r) => (
                  <span className="num">
                    {fmtAd(nptDate(r.occurred_at))}, {fmtTime(r.occurred_at)}
                  </span>
                ),
              },
              { key: 'action', header: 'Action', render: (r) => humanize(r.action) },
              {
                key: 'table_name',
                header: 'Record',
                render: (r) => (
                  <Text size="sm" ff="monospace" c="dimmed">
                    {r.table_name}
                  </Text>
                ),
              },
              { key: 'actor_id', header: 'By', render: (r) => r.actor_id ?? 'System' },
            ]}
          />
        </Tabs.Panel>
      </Tabs>

      <Drawer opened={editing} onClose={() => setEditing(false)} title="Edit profile" position="right" size="lg">
        <EmployeeForm
          initial={e}
          busy={busy}
          onSubmit={async (draft) => {
            setBusy(true);
            try {
              await empApi.update(id, draft);
              toast.ok('Changes saved.');
              setEditing(false);
              await emp.reload();
            } catch (err) {
              toast.err(err);
            } finally {
              setBusy(false);
            }
          }}
        />
      </Drawer>
      <ConfirmDialog
        opened={shred}
        onClose={() => setShred(false)}
        title="Shred all biometric templates?"
        confirmLabel="Shred templates"
        message={`Every stored template for ${e.name_en} will be permanently destroyed and their consents withdrawn. They will need to enrol again to use face or fingerprint.`}
        onConfirm={async () => {
          try {
            const r = await bioApi.shredAll(id);
            toast.ok(`${r.shredded} ${r.shredded === 1 ? 'template' : 'templates'} shredded.`);
            await Promise.all([templates.reload(), consents.reload()]);
          } catch (err) {
            toast.err(err);
          }
        }}
      />
    </Stack>
  );
}
