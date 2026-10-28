import {
  Alert,
  Badge,
  Box,
  Button,
  Checkbox,
  Drawer,
  Group,
  Loader,
  Paper,
  Select,
  Stack,
  Switch,
  Text,
  Textarea,
  TextInput,
} from '@mantine/core';
import { IconAlertTriangle, IconBan, IconEye, IconMail, IconPrinter } from '@tabler/icons-react';
import { useEffect, useMemo, useState } from 'react';
import { ApiError } from '../../api/client';
import { documents as docsApi, nptDateOf } from '../../api/documents';
import { DataTable } from '../../components/DataTable';
import { EmployeePicker } from '../../components/EmployeePicker';
import { fmtDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const ICON = { size: 16, stroke: 1.6 };

const REQ_STATUS = {
  pending: { label: 'Waiting for HR', color: 'yellow' },
  approved: { label: 'Being prepared', color: 'blue' },
  issued: { label: 'Issued', color: 'brand' },
  rejected: { label: 'Declined', color: 'red' },
  cancelled: { label: 'Cancelled', color: 'gray' },
};
export function RequestStatus({ status }) {
  const s = REQ_STATUS[status];
  return (
    <Badge variant="light" color={s.color} size="sm">
      {s.label}
    </Badge>
  );
}

/** The printed letter as it will look, rendered from the server's HTML in a sandboxed frame (no scripts). */
export function LetterFrame({ html, height = 640 }) {
  return (
    <Box style={{ border: '1px solid var(--hj-line)', borderRadius: 12, overflow: 'hidden', background: '#fff' }}>
      <iframe
        title="Letter preview"
        sandbox=""
        srcDoc={html}
        style={{ width: '100%', height, border: 0, display: 'block', background: '#fff' }}
      />
    </Box>
  );
}

/**
 * Issue a letter from a template: pick the employee (unless fixed), the template and any request details, preview it
 * with missing details highlighted, then issue it with the next reference number.
 */
export function IssueLetterDrawer({ opened, onClose, employeeId, request, onIssued }) {
  const tpls = useQuery(() => docsApi.templates(), [], { enabled: opened });
  const ph = useQuery(() => docsApi.placeholders(), [], { enabled: opened });
  const [emp, setEmp] = useState(null);
  const [templateId, setTemplateId] = useState(null);
  const [purpose, setPurpose] = useState('');
  const [addressedTo, setAddressedTo] = useState('');
  const [visible, setVisible] = useState(true);
  const [allowMissing, setAllowMissing] = useState(false);
  const [preview, setPreview] = useState(null);
  const [loading, setLoading] = useState(false);
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!opened) return;
    setEmp(request?.employee_id ?? employeeId ?? null);
    setTemplateId(request?.template_id ?? null);
    setPurpose(request?.purpose ?? '');
    setAddressedTo(request?.addressed_to ?? '');
    setAllowMissing(false);
    setPreview(null);
  }, [opened, request, employeeId]);

  const templates = (tpls.data?.templates ?? []).filter((t) => t.active !== false);
  const tpl = useMemo(() => templates.find((t) => t.id === templateId), [templates, templateId]);
  useEffect(() => {
    if (tpl) setVisible(tpl.visible_to_employee !== false);
  }, [tpl]);
  const usesPurpose = !!tpl?.body && /\{\{\s*purpose\s*\}\}/.test(tpl.body + tpl.subject);
  const usesAddressee = !!tpl?.body && /\{\{\s*addressed_to\s*\}\}/.test(tpl.body + tpl.subject);

  // Live preview whenever the inputs settle.
  useEffect(() => {
    if (!opened || !emp || !templateId) {
      setPreview(null);
      return;
    }
    let alive = true;
    const t = setTimeout(async () => {
      setLoading(true);
      try {
        const r = await docsApi.preview({
          employeeId: emp,
          templateId,
          purpose: purpose.trim() || null,
          addressedTo: addressedTo.trim() || null,
        });
        if (alive) setPreview({ html: r.html, missing: r.missing });
      } catch (e) {
        if (alive) {
          setPreview(null);
          toast.err(e, 'Preview failed');
        }
      } finally {
        if (alive) setLoading(false);
      }
    }, 350);
    return () => {
      alive = false;
      clearTimeout(t);
    };
  }, [opened, emp, templateId, purpose, addressedTo]);

  const issue = async () => {
    if (!emp || !templateId) return;
    setBusy(true);
    try {
      const r = await docsApi.issue({
        templateId,
        employeeId: emp,
        visibleToEmployee: visible,
        purpose: purpose.trim() || null,
        addressedTo: addressedTo.trim() || null,
        ...(request ? { requestId: request.id } : {}),
        allowMissing,
      });
      toast.ok(`Letter ${r.refNo} issued.`);
      window.open(docsApi.letterUrl(r.id), '_blank', 'noopener');
      onIssued?.();
      onClose();
    } catch (e) {
      if (e instanceof ApiError && e.details?.code === 'missing_values') toast.err(e, 'Some details are missing');
      else toast.err(e);
    } finally {
      setBusy(false);
    }
  };

  const missing = preview?.missing ?? [];
  const label = (k) => ph.data?.placeholders.find((p) => p.key === k)?.label.toLowerCase() ?? k;
  return (
    <Drawer
      opened={opened}
      onClose={onClose}
      position="right"
      size="xl"
      title={<Text fw={600}>{request ? `Issue ${request.template_name.toLowerCase()} for ${request.name_en}` : 'Issue a letter'}</Text>}
    >
      <Stack gap="md">
        {!employeeId && !request ? <EmployeePicker value={emp} onChange={(v) => setEmp(v)} required status="all" /> : null}
        <Select
          label="Letter"
          required
          searchable
          value={templateId}
          onChange={setTemplateId}
          disabled={!!request}
          allowDeselect={false}
          data={templates.map((t) => ({ value: t.id, label: t.name }))}
          placeholder={tpls.loading ? 'Loading templates…' : 'Choose a template'}
        />
        {usesPurpose || request ? (
          <TextInput
            label="Purpose"
            value={purpose}
            onChange={(e) => setPurpose(e.currentTarget.value)}
            maxLength={300}
            placeholder="e.g. visa application"
          />
        ) : null}
        {usesAddressee || request?.addressed_to ? (
          <TextInput
            label="Addressed to"
            value={addressedTo}
            onChange={(e) => setAddressedTo(e.currentTarget.value)}
            maxLength={200}
            placeholder="e.g. The Manager, ABC Bank"
          />
        ) : null}
        <Switch label="Employee can see and print this letter" checked={visible} onChange={(e) => setVisible(e.currentTarget.checked)} />
        {missing.length ? (
          <Alert variant="light" color="orange" icon={<IconAlertTriangle {...ICON} />} title="Some details are missing">
            <Text size="sm">Fill these in on the employee's profile, or issue with them left blank: {missing.map(label).join(', ')}.</Text>
            <Checkbox
              mt="xs"
              label="Issue anyway with these left blank"
              checked={allowMissing}
              onChange={(e) => setAllowMissing(e.currentTarget.checked)}
            />
          </Alert>
        ) : null}
        {loading && !preview ? (
          <Group justify="center" p="xl">
            <Loader size="sm" />
          </Group>
        ) : null}
        {preview ? (
          <LetterFrame html={preview.html} height={560} />
        ) : emp && templateId ? null : (
          <Text size="sm" c="dimmed">
            Choose an employee and a letter to see a preview.
          </Text>
        )}
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button
            leftSection={<IconPrinter {...ICON} />}
            loading={busy}
            disabled={!emp || !templateId || (missing.length > 0 && !allowMissing)}
            onClick={issue}
          >
            Issue letter
          </Button>
        </Group>
      </Stack>
    </Drawer>
  );
}

/** Issued letters: reference, subject, date; HR also sees the employee and can void a letter. */
export function LetterTable({ letters, loading, error, hr, showEmployee, onChanged }) {
  const [voiding, setVoiding] = useState(null);
  const [reason, setReason] = useState('');
  const doVoid = async () => {
    if (!voiding) return;
    try {
      await docsApi.voidLetter(voiding.id, reason.trim());
      toast.ok(`${voiding.ref_no} voided.`);
      setVoiding(null);
      setReason('');
      onChanged?.();
    } catch (e) {
      toast.err(e);
    }
  };
  return (
    <>
      <DataTable
        rows={letters}
        rowKey={(l) => l.id}
        loading={loading ?? false}
        error={error ?? null}
        emptyTitle="No letters yet"
        emptyHint={
          hr
            ? 'Issue an appointment, experience or salary letter from a template.'
            : 'Letters HR issues to you, such as a salary certificate, appear here.'
        }
        columns={[
          {
            key: 'ref',
            header: 'Reference',
            render: (l) => (
              <Text size="sm" ff="monospace" td={l.voided_at ? 'line-through' : undefined}>
                {l.ref_no}
              </Text>
            ),
          },
          ...(showEmployee
            ? [
                {
                  key: 'emp',
                  header: 'Employee',
                  render: (l) => (
                    <div>
                      <Text size="sm">{l.name_en}</Text>
                      <Text size="xs" c="dimmed">
                        {l.emp_code}
                      </Text>
                    </div>
                  ),
                },
              ]
            : []),
          {
            key: 'subject',
            header: 'Letter',
            render: (l) => (
              <Group gap={6} wrap="nowrap">
                <Text size="sm">{l.subject}</Text>
                {l.voided_at ? (
                  <Badge size="xs" color="red" variant="light">
                    Void
                  </Badge>
                ) : null}
                {hr && !l.visible_to_employee ? (
                  <Badge size="xs" color="gray" variant="light">
                    HR only
                  </Badge>
                ) : null}
              </Group>
            ),
          },
          { key: 'date', header: 'Issued', render: (l) => <Text size="sm">{fmtDate(nptDateOf(l.issued_at))}</Text> },
          {
            key: 'act',
            header: '',
            align: 'right',
            render: (l) => (
              <Group gap={4} justify="flex-end" wrap="nowrap">
                <Button
                  size="compact-sm"
                  variant="subtle"
                  leftSection={<IconEye size={14} />}
                  onClick={(e) => {
                    e.stopPropagation();
                    window.open(docsApi.letterUrl(l.id), '_blank', 'noopener');
                  }}
                >
                  Open
                </Button>
                {hr && !l.voided_at ? (
                  <Button
                    size="compact-sm"
                    variant="subtle"
                    color="red"
                    leftSection={<IconBan size={14} />}
                    onClick={(e) => {
                      e.stopPropagation();
                      setVoiding(l);
                      setReason('');
                    }}
                  >
                    Void
                  </Button>
                ) : null}
              </Group>
            ),
          },
        ]}
      />
      <Drawer
        opened={!!voiding}
        onClose={() => setVoiding(null)}
        position="right"
        size="sm"
        title={<Text fw={600}>Void {voiding?.ref_no}</Text>}
      >
        <Stack gap="md">
          <Text size="sm">
            A voided letter keeps its reference number but is marked VOID and hidden from the employee. Issue a corrected letter afterwards
            if needed.
          </Text>
          <Textarea
            label="Reason"
            required
            autosize
            minRows={2}
            maxLength={300}
            value={reason}
            onChange={(e) => setReason(e.currentTarget.value)}
          />
          <Group justify="flex-end">
            <Button variant="default" onClick={() => setVoiding(null)}>
              Cancel
            </Button>
            <Button color="red" disabled={reason.trim().length < 3} onClick={doVoid}>
              Void letter
            </Button>
          </Group>
        </Stack>
      </Drawer>
    </>
  );
}

/** Employee: ask HR for a letter such as a salary certificate. */
export function RequestLetterDrawer({ opened, onClose, onDone }) {
  const tpls = useQuery(() => docsApi.templates(), [], { enabled: opened });
  const [templateId, setTemplateId] = useState(null);
  const [purpose, setPurpose] = useState('');
  const [addressedTo, setAddressedTo] = useState('');
  const [busy, setBusy] = useState(false);
  useEffect(() => {
    if (opened) {
      setTemplateId(null);
      setPurpose('');
      setAddressedTo('');
    }
  }, [opened]);
  const submit = async () => {
    if (!templateId) return;
    setBusy(true);
    try {
      await docsApi.requestLetter({ templateId, purpose: purpose.trim(), addressedTo: addressedTo.trim() || null });
      toast.ok('Request sent to HR.');
      onDone();
      onClose();
    } catch (e) {
      toast.err(e);
    } finally {
      setBusy(false);
    }
  };
  return (
    <Drawer opened={opened} onClose={onClose} position="right" size="md" title={<Text fw={600}>Request a letter</Text>}>
      <Stack gap="md">
        <Select
          label="Letter"
          required
          data={(tpls.data?.templates ?? []).map((t) => ({ value: t.id, label: t.name }))}
          value={templateId}
          onChange={setTemplateId}
          allowDeselect={false}
          placeholder={tpls.loading ? 'Loading…' : 'Choose a letter'}
          nothingFoundMessage="HR has not made any letters requestable"
        />
        <Textarea
          label="What is it for?"
          required
          autosize
          minRows={2}
          maxLength={300}
          value={purpose}
          onChange={(e) => setPurpose(e.currentTarget.value)}
          placeholder="e.g. Home loan application at ABC Bank"
        />
        <TextInput
          label="Addressed to (optional)"
          maxLength={200}
          value={addressedTo}
          onChange={(e) => setAddressedTo(e.currentTarget.value)}
          placeholder="e.g. The Manager, ABC Bank, Kathmandu"
        />
        <Text size="xs" c="dimmed">
          HR prepares the letter with a reference number. You will get a notification when it is ready to print.
        </Text>
        <Group justify="flex-end">
          <Button variant="default" onClick={onClose}>
            Cancel
          </Button>
          <Button leftSection={<IconMail {...ICON} />} loading={busy} disabled={!templateId || purpose.trim().length < 3} onClick={submit}>
            Send request
          </Button>
        </Group>
      </Stack>
    </Drawer>
  );
}

/** A compact list of letter requests with their status. */
export function RequestList({ requests, hr, onIssue, onCancel }) {
  return (
    <Stack gap="xs">
      {requests.map((r) => (
        <Paper key={r.id} withBorder radius="md" p="sm">
          <Group justify="space-between" wrap="wrap" gap="xs">
            <div style={{ minWidth: 0 }}>
              <Group gap={8}>
                <Text size="sm" fw={600}>
                  {r.template_name}
                </Text>
                <RequestStatus status={r.status} />
              </Group>
              <Text size="xs" c="dimmed">
                {hr ? `${r.name_en} (${r.emp_code}) · ` : ''}Requested {fmtDate(nptDateOf(r.created_at))} · {r.purpose}
              </Text>
              {r.status === 'rejected' && r.decision_reason ? (
                <Text size="xs" c="red.7">
                  “{r.decision_reason}”
                </Text>
              ) : null}
            </div>
            <Group gap={6}>
              {r.status === 'issued' && r.issued_letter_id ? (
                <Button
                  size="compact-sm"
                  variant="light"
                  leftSection={<IconPrinter size={14} />}
                  onClick={() => window.open(docsApi.letterUrl(r.issued_letter_id), '_blank', 'noopener')}
                >
                  Open letter
                </Button>
              ) : null}
              {hr && (r.status === 'pending' || r.status === 'approved') && onIssue ? (
                <Button size="compact-sm" onClick={() => onIssue(r)}>
                  Prepare letter
                </Button>
              ) : null}
              {!hr && (r.status === 'pending' || r.status === 'approved') && onCancel ? (
                <Button size="compact-sm" variant="subtle" color="gray" onClick={() => onCancel(r)}>
                  Cancel
                </Button>
              ) : null}
            </Group>
          </Group>
        </Paper>
      ))}
    </Stack>
  );
}
