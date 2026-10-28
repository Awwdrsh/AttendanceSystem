import { Badge, Button, Group, Modal, Paper, Select, Stack, Text, Textarea } from '@mantine/core';
import { IconHandStop } from '@tabler/icons-react';
import { useState } from 'react';
import { compliance } from '../../../api/compliance';
import { fmtMoney } from '../../../lib/format';
import { toast } from '../../../lib/toast';
import { useQuery } from '../../../lib/useQuery';

/**
 * Pay run review step: hold someone's net pay in this run (it is calculated, taxed and shown on the payslip, but not
 * paid until HR releases it under Payroll, Held pay).
 */
export function RunHolds({ runId, slips, canHold }) {
  const q = useQuery(() => compliance.holds('open'), [runId]);
  const [open, setOpen] = useState(false);
  const [emp, setEmp] = useState(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const mine = (q.data?.holds ?? []).filter((h) => h.run_id === runId);
  if (!slips.length) return null;
  const held = new Set(mine.map((h) => h.employee_id));
  const options = slips.flatMap((s) =>
    s.employee_id && !held.has(s.employee_id) && Number(s.net) > 0
      ? [{ value: s.employee_id, label: `${s.name_en ?? ''} (${s.emp_code ?? ''}) · ${fmtMoney(s.net)}` }]
      : [],
  );
  const hold = async () => {
    if (!emp) return;
    setBusy(true);
    try {
      await compliance.hold(runId, emp, reason.trim());
      toast.ok('Pay held. Release it under Payroll, Held pay once the issue is settled.');
      setOpen(false);
      setEmp(null);
      setReason('');
      await q.reload();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Paper withBorder p="md">
      <Group justify="space-between" wrap="wrap" gap="sm">
        <div style={{ minWidth: 0 }}>
          <Group gap={8}>
            <IconHandStop size={16} stroke={1.6} />
            <Text fw={600} size="sm">
              Held pay
            </Text>
            {mine.length ? (
              <Badge variant="light" color="orange">
                {mine.length}
              </Badge>
            ) : null}
          </Group>
          {mine.length ? (
            <Stack gap={2} mt={6}>
              {mine.map((h) => (
                <Text key={h.id} size="sm">
                  {h.name_en}{' '}
                  <Text span c="dimmed" size="xs">
                    ({h.emp_code})
                  </Text>
                  : <span className="num">{fmtMoney(h.amount)}</span>{' '}
                  <Text span c="dimmed" size="xs">
                    · {h.reason}
                  </Text>
                </Text>
              ))}
            </Stack>
          ) : (
            <Text size="sm" c="dimmed">
              Everyone in this run is paid. Hold someone's net pay if, for example, their documents are pending.
            </Text>
          )}
        </div>
        {canHold ? (
          <Button variant="default" size="sm" onClick={() => setOpen(true)}>
            Hold pay
          </Button>
        ) : null}
      </Group>
      <Modal opened={open} onClose={() => setOpen(false)} title="Hold pay in this run" centered>
        <Stack>
          <Select label="Employee" searchable value={emp} onChange={(v) => setEmp(v)} required data={options} />
          <Textarea
            label="Reason"
            description="Kept for HR; the employee sees only that the payment is on hold."
            value={reason}
            onChange={(e) => setReason(e.currentTarget.value)}
            autosize
            minRows={2}
            maxLength={500}
            required
          />
          <Text size="xs" c="dimmed">
            The payslip, tax and SSF stay as calculated. The net pay is paid later, when you release it.
          </Text>
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setOpen(false)}>
              Cancel
            </Button>
            <Button color="orange" loading={busy} disabled={!emp || reason.trim().length < 3} onClick={hold}>
              Hold pay
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Paper>
  );
}
