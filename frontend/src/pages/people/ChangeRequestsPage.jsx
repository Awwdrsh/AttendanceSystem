import { Anchor, Badge, Button, Group, Modal, Paper, SegmentedControl, Skeleton, Stack, Text, Textarea } from '@mantine/core';
import { IconCheck, IconEye, IconPaperclip, IconUserEdit, IconX } from '@tabler/icons-react';
import { useState } from 'react';
import { Link } from 'react-router-dom';
import { workflow as wfApi } from '../../api';
import { people, proofUrl } from '../../api/people';
import { useAuth } from '../../auth/AuthProvider';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { ChangeList } from '../../components/people/ChangeList';
import { fmtAd, NPT_OFFSET_MIN } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const STATUS = {
  pending: { label: 'Waiting', color: 'yellow' },
  approved: { label: 'Approved', color: 'green' },
  rejected: { label: 'Not approved', color: 'red' },
  cancelled: { label: 'Cancelled', color: 'gray' },
};
const nptDate = (iso) => new Date(Date.parse(iso) + NPT_OFFSET_MIN * 60_000).toISOString().slice(0, 10);

function RequestCard({ r, onDone }) {
  const { user } = useAuth();
  const [revealed, setRevealed] = useState(null);
  const [rejecting, setRejecting] = useState(false);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const ownRequest = r.requested_by === user?.id;
  const approve = async () => {
    if (!r.instance_id) return;
    setBusy(true);
    try {
      await wfApi.approve(r.instance_id);
      toast.ok(`${r.sectionLabel} updated for ${r.employee_name}.`);
      onDone();
    } catch (e) {
      toast.err(e, 'Not approved');
    } finally {
      setBusy(false);
    }
  };
  const reject = async () => {
    if (!r.instance_id || !reason.trim()) return;
    setBusy(true);
    try {
      await wfApi.reject(r.instance_id, reason.trim());
      toast.ok('Request declined. The employee has been told why.');
      setRejecting(false);
      onDone();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Paper withBorder p="lg">
      <Group justify="space-between" wrap="wrap" gap="sm" mb="sm">
        <div>
          <Group gap="xs">
            <Anchor component={Link} to={`/employees/${r.employee_id}`} fw={600} size="sm">
              {r.employee_name}
            </Anchor>
            <Text size="xs" c="dimmed" ff="monospace">
              {r.emp_code}
            </Text>
          </Group>
          <Text size="sm">
            {r.sectionLabel}
            {r.summary.byHr ? (
              <Badge ml={6} size="xs" variant="light" color="grape">
                HR edit, needs second approval
              </Badge>
            ) : null}
          </Text>
          <Text size="xs" c="dimmed">
            Requested {fmtAd(nptDate(r.created_at))}
            {r.requested_by_name ? ` by ${r.requested_by_name}` : ''}
          </Text>
        </div>
        <Badge variant="light" color={STATUS[r.status]?.color ?? 'gray'}>
          {STATUS[r.status]?.label ?? r.status}
        </Badge>
      </Group>
      <ChangeList changes={revealed ?? r.summary.changes ?? []} />
      {r.reason ? (
        <Text size="sm" c="dimmed" mt="xs">
          “{r.reason}”
        </Text>
      ) : null}
      {r.status === 'rejected' && r.decision_reason ? (
        <Text size="sm" c="red.7" mt="xs">
          Declined: {r.decision_reason}
        </Text>
      ) : null}
      <Group justify="space-between" mt="md" wrap="wrap" gap="xs">
        <Group gap="xs">
          {r.proof_file_id ? (
            <Button
              size="xs"
              variant="default"
              component="a"
              href={proofUrl(r.id)}
              target="_blank"
              rel="noopener"
              leftSection={<IconPaperclip size={14} stroke={1.6} />}
            >
              View proof
            </Button>
          ) : null}
          {r.status === 'pending' && !revealed && ['bank', 'ids'].includes(r.section) ? (
            <Button
              size="xs"
              variant="subtle"
              leftSection={<IconEye size={14} stroke={1.6} />}
              onClick={async () => {
                try {
                  setRevealed((await people.request(r.id, true)).changes);
                } catch (e) {
                  toast.err(e);
                }
              }}
            >
              Show full numbers
            </Button>
          ) : null}
        </Group>
        {r.status === 'pending' && r.instance_id ? (
          ownRequest ? (
            <Text size="xs" c="dimmed">
              You made this change; another HR person must approve it.
            </Text>
          ) : (
            <Group gap="xs">
              <Button
                size="xs"
                variant="default"
                color="red"
                leftSection={<IconX size={14} stroke={1.6} />}
                onClick={() => setRejecting(true)}
                disabled={busy}
              >
                Decline
              </Button>
              <Button size="xs" leftSection={<IconCheck size={14} stroke={1.6} />} onClick={approve} loading={busy}>
                Approve and apply
              </Button>
            </Group>
          )
        ) : null}
      </Group>
      <Modal opened={rejecting} onClose={() => setRejecting(false)} title="Decline this change?" centered>
        <Stack>
          <Textarea
            label="Reason"
            description="The employee sees this."
            required
            autosize
            minRows={2}
            value={reason}
            onChange={(e) => setReason(e.currentTarget.value)}
            data-autofocus
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setRejecting(false)}>
              Cancel
            </Button>
            <Button color="red" onClick={reject} loading={busy} disabled={!reason.trim()}>
              Decline
            </Button>
          </Group>
        </Stack>
      </Modal>
    </Paper>
  );
}

/** HR reviews profile changes that employees asked for (and bank or ID edits waiting for a second HR approval). */
export default function ChangeRequestsPage() {
  const [status, setStatus] = useState('pending');
  const q = useQuery(() => people.requests(status), [status]);
  return (
    <Stack gap="lg">
      <PageHeader
        title="Profile change requests"
        subtitle="Check each change against the proof, then approve it to update the employee's record."
        actions={
          <SegmentedControl
            size="xs"
            value={status}
            onChange={(v) => setStatus(v)}
            data={[
              { value: 'pending', label: 'Waiting' },
              { value: 'approved', label: 'Approved' },
              { value: 'rejected', label: 'Declined' },
              { value: 'all', label: 'All' },
            ]}
          />
        }
      />
      {q.loading && !q.data ? (
        <Stack>
          {[0, 1].map((i) => (
            <Skeleton key={i} h={140} radius="md" />
          ))}
        </Stack>
      ) : q.error ? (
        <EmptyState title="We could not load the requests" hint={q.error} action={{ label: 'Try again', onClick: () => void q.reload() }} />
      ) : !q.data?.requests.length ? (
        <EmptyState
          icon={<IconUserEdit size={20} stroke={1.6} />}
          title={status === 'pending' ? 'Nothing waiting' : 'No requests here'}
          hint="When employees ask to change their details, the requests appear here and in Approvals."
        />
      ) : (
        <Stack gap="md">
          {q.data.requests.map((r) => (
            <RequestCard key={r.id} r={r} onDone={() => void q.reload()} />
          ))}
        </Stack>
      )}
    </Stack>
  );
}
