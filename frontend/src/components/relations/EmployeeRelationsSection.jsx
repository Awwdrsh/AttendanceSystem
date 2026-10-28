import { Badge, Button, Drawer, Group, Paper, Progress, SimpleGrid, Stack, Text } from '@mantine/core';
import { IconPlus } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { CASE_CATEGORY_LABEL, KYE_SECTION_LABEL, relations } from '../../api/relations';
import { DateField } from '../BsDatePicker';
import { SectionTitle } from '../PageHeader';
import { fmtAd } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';
import { CaseDetail, CaseStatusBadge, OpenCase } from '../../pages/relations/DisciplinaryPage';
import { AssignmentForm, assignmentWhere } from '../../pages/relations/TempAssignmentsPage';

/** HR-only tab on the employee page: contract end and retirement, temporary roles, record completeness and disciplinary history. */
export function EmployeeRelationsSection({ employeeId }) {
  const contract = useQuery(() => relations.contract(employeeId), [employeeId]);
  const assignments = useQuery(() => relations.assignments({ employeeId, status: 'all' }), [employeeId]);
  const cases = useQuery(() => relations.cases({ employeeId }), [employeeId]);
  const comp = useQuery(() => relations.completeness(), [employeeId]);
  const [end, setEnd] = useState(null);
  const [adding, setAdding] = useState(false);
  const [opening, setOpening] = useState(false);
  const [openId, setOpenId] = useState(null);
  useEffect(() => {
    setEnd(contract.data?.contractEnd ?? null);
  }, [contract.data]);
  const mine = comp.data?.employees.find((e) => e.employeeId === employeeId);
  return (
    <Stack gap="lg">
      <SimpleGrid cols={{ base: 1, sm: 2 }}>
        <Paper withBorder p="md">
          <Text size="sm" fw={600} mb="xs">
            Contract and retirement
          </Text>
          <Group align="flex-end">
            <DateField label="Contract ends" value={end} onChange={setEnd} />
            <Button
              size="xs"
              variant="default"
              disabled={end === (contract.data?.contractEnd ?? null)}
              onClick={async () => {
                try {
                  await relations.setContract(employeeId, end);
                  toast.ok('Saved.');
                  await contract.reload();
                } catch (e) {
                  toast.err(e);
                }
              }}
            >
              Save
            </Button>
          </Group>
          <Text size="xs" c="dimmed" mt="xs">
            {contract.data?.retirementDate
              ? `Reaches retirement age (${contract.data.retirementAge}) on ${fmtAd(contract.data.retirementDate)}.`
              : 'Add the date of birth to see the retirement date.'}
          </Text>
        </Paper>
        <Paper withBorder p="md">
          <Text size="sm" fw={600} mb="xs">
            Record completeness
          </Text>
          {mine ? (
            <>
              <Group gap="xs">
                <Progress style={{ flex: 1 }} value={mine.percent} color={mine.percent < 100 ? 'orange' : 'teal'} />
                <Text size="sm">{mine.percent}%</Text>
              </Group>
              {mine.missing.length ? (
                <Group gap={4} mt="xs">
                  {mine.missing.map((m) => (
                    <Badge key={m} size="xs" variant="light" color="gray">
                      {KYE_SECTION_LABEL[m]}
                    </Badge>
                  ))}
                </Group>
              ) : (
                <Text size="xs" c="dimmed" mt="xs">
                  Nothing missing.
                </Text>
              )}
            </>
          ) : (
            <Text size="xs" c="dimmed">
              Only active employees are measured.
            </Text>
          )}
        </Paper>
      </SimpleGrid>
      <div>
        <SectionTitle
          actions={
            <Button size="xs" variant="default" leftSection={<IconPlus size={14} />} onClick={() => setAdding(true)}>
              Add
            </Button>
          }
        >
          Acting and deputation
        </SectionTitle>
        {(assignments.data?.assignments ?? []).length ? (
          <Paper withBorder p="sm">
            <Stack gap={6}>
              {assignments.data.assignments.map((a) => (
                <Group key={a.id} justify="space-between">
                  <Text size="sm">
                    {a.kind === 'acting' ? 'Acting as' : 'Deputed to'} {assignmentWhere(a)}
                  </Text>
                  <Group gap="xs">
                    <Text size="xs" c="dimmed">
                      {fmtAd(a.start_date)} to {fmtAd(a.end_date)}
                    </Text>
                    <Badge size="xs" variant="light" color={a.status === 'active' ? 'teal' : 'gray'}>
                      {a.status}
                    </Badge>
                  </Group>
                </Group>
              ))}
            </Stack>
          </Paper>
        ) : (
          <Text size="sm" c="dimmed">
            None.
          </Text>
        )}
      </div>
      <div>
        <SectionTitle
          hint="HR only."
          actions={
            <Button size="xs" variant="default" leftSection={<IconPlus size={14} />} onClick={() => setOpening(true)}>
              Open case
            </Button>
          }
        >
          Disciplinary history
        </SectionTitle>
        {(cases.data?.cases ?? []).length ? (
          <Paper withBorder p="sm">
            <Stack gap={6}>
              {cases.data.cases.map((k) => (
                <Group key={k.id} justify="space-between" style={{ cursor: 'pointer' }} onClick={() => setOpenId(k.id)}>
                  <Text size="sm">
                    D-{k.number}, {CASE_CATEGORY_LABEL[k.category]} on {fmtAd(k.incidentDate)}
                    {k.stageName ? `: ${k.stageName}` : ''}
                  </Text>
                  <CaseStatusBadge status={k.status} />
                </Group>
              ))}
            </Stack>
          </Paper>
        ) : (
          <Text size="sm" c="dimmed">
            No cases.
          </Text>
        )}
      </div>
      <AssignmentForm
        opened={adding}
        onClose={() => setAdding(false)}
        onSaved={() => {
          void assignments.reload();
        }}
        employeeId={employeeId}
      />
      <OpenCase
        opened={opening}
        onClose={() => setOpening(false)}
        employeeId={employeeId}
        onCreated={(id) => {
          setOpening(false);
          void cases.reload();
          setOpenId(id);
        }}
      />
      <Drawer opened={!!openId} onClose={() => setOpenId(null)} position="right" size="lg" title={<Text fw={600}>Disciplinary case</Text>}>
        {openId ? (
          <CaseDetail
            id={openId}
            onChanged={() => {
              void cases.reload();
            }}
          />
        ) : null}
      </Drawer>
    </Stack>
  );
}
