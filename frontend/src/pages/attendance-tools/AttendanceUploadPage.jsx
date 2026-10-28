import { Alert, Badge, Button, FileButton, Group, Paper, Stack, Text, TextInput } from '@mantine/core';
import { IconDownload, IconFileUpload, IconUpload } from '@tabler/icons-react';
import { useState } from 'react';
import { attendanceTools } from '../../api/attendanceTools';
import { download } from '../../api/client';
import { DataTable } from '../../components/DataTable';
import { StatStrip } from '../../components/Kpi';
import { SectionTitle } from '../../components/PageHeader';
import { fmtDateShort, relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/**
 * HR: upload attendance from a spreadsheet (for example when a device was offline). Every row is checked first; nothing is
 * saved until all rows are clean. Re-uploading the same rows adds nothing.
 */
export default function AttendanceUploadPage() {
  const [file, setFile] = useState(null);
  const [check, setCheck] = useState(null);
  const [reason, setReason] = useState('');
  const [busy, setBusy] = useState(false);
  const history = useQuery(() => attendanceTools.uploads(), []);

  const pick = async (f) => {
    if (!f) return;
    if (f.size > 900_000) {
      toast.err(new Error('The file is too large. Upload at most 2,000 rows at a time.'));
      return;
    }
    const csv = await f.text();
    setFile({ name: f.name, csv });
    setCheck(null);
    setBusy(true);
    try {
      setCheck(await attendanceTools.checkUpload(csv));
    } catch (e) {
      toast.err(e, 'Could not read the file');
    } finally {
      setBusy(false);
    }
  };
  const importNow = async () => {
    if (!file) return;
    setBusy(true);
    try {
      const r = await attendanceTools.importUpload(file.csv, reason.trim(), file.name);
      toast.ok(
        `${r.punches} punch(es) added for ${r.days} day(s)${r.duplicates ? `; ${r.duplicates} were already there` : ''}.`,
        'Attendance uploaded',
      );
      setFile(null);
      setCheck(null);
      setReason('');
      await history.reload();
    } catch (e) {
      toast.err(e, 'Not uploaded');
    } finally {
      setBusy(false);
    }
  };

  const rows = check?.rows ?? [];
  return (
    <Stack gap="xl">
      <Paper withBorder radius="lg" p="lg">
        <SectionTitle
          hint="One row per person per day: emp_code, date_bs (YYYY-MM-DD in Bikram Sambat), in_time and out_time (24-hour HH:MM) and a note. An out time earlier than the in time is read as the next morning."
          actions={
            <Button
              size="xs"
              variant="default"
              leftSection={<IconDownload size={14} stroke={1.6} />}
              onClick={() => download('/attendance-tools/upload/template', 'attendance-upload-template.csv').catch((e) => toast.err(e))}
            >
              Template
            </Button>
          }
        >
          Upload attendance
        </SectionTitle>
        <Group gap="sm">
          <FileButton accept=".csv,text/csv" onChange={(f) => void pick(f)}>
            {(props) => (
              <Button
                {...props}
                leftSection={<IconFileUpload size={16} stroke={1.6} />}
                loading={busy && !check}
                variant={file ? 'default' : 'filled'}
              >
                {file ? 'Choose another file' : 'Choose CSV file'}
              </Button>
            )}
          </FileButton>
          {file ? (
            <Text size="sm" c="dimmed">
              {file.name}
            </Text>
          ) : null}
        </Group>
      </Paper>

      {check ? (
        <Stack gap="md">
          {check.errors.length ? (
            <Alert color="red" variant="light" title="The file cannot be read">
              {check.errors.join(' ')}
            </Alert>
          ) : null}
          {rows.length ? (
            <>
              <StatStrip
                stats={[
                  { key: 'rows', label: 'Rows', value: rows.length },
                  { key: 'ok', label: 'Ready', value: check.valid },
                  { key: 'bad', label: 'With errors', value: check.invalid, ...(check.invalid ? { tone: 'bad' } : {}) },
                ]}
              />
              {check.invalid ? (
                <Alert color="orange" variant="light">
                  Fix the rows marked in red in your file and choose it again. Nothing has been saved yet.
                </Alert>
              ) : (
                <Paper withBorder radius="lg" p="md">
                  <Group align="flex-end" gap="sm">
                    <TextInput
                      label="Reason"
                      description="Saved with every punch for the audit trail"
                      placeholder="For example: front door device offline"
                      value={reason}
                      onChange={(e) => setReason(e.currentTarget.value)}
                      style={{ flex: 1, minWidth: 240 }}
                    />
                    <Button
                      leftSection={<IconUpload size={16} stroke={1.6} />}
                      disabled={reason.trim().length < 5}
                      loading={busy}
                      onClick={importNow}
                    >
                      Add {check.valid} row(s)
                    </Button>
                  </Group>
                </Paper>
              )}
              <DataTable
                rows={rows}
                rowKey={(r) => String(r.line)}
                highlight={(r) => r.errors.length > 0}
                maxHeight={480}
                columns={[
                  {
                    key: 'line',
                    header: 'Row',
                    width: 60,
                    render: (r) => (
                      <Text size="sm" c="dimmed" className="num">
                        {r.line}
                      </Text>
                    ),
                  },
                  {
                    key: 'emp',
                    header: 'Employee',
                    render: (r) => (
                      <div>
                        <Text size="sm" fw={500}>
                          {r.name ?? r.empCode}
                        </Text>
                        {r.name ? (
                          <Text size="xs" c="dimmed">
                            {r.empCode}
                          </Text>
                        ) : null}
                      </div>
                    ),
                  },
                  {
                    key: 'date',
                    header: 'Date',
                    render: (r) =>
                      r.date ? (
                        fmtDateShort(r.date)
                      ) : (
                        <Text size="sm" c="dimmed">
                          {r.dateBs ?? '—'}
                        </Text>
                      ),
                  },
                  { key: 'in', header: 'In', render: (r) => <span className="num">{r.inTime ?? '—'}</span> },
                  {
                    key: 'out',
                    header: 'Out',
                    render: (r) => (
                      <Group gap={4}>
                        <span className="num">{r.outTime ?? '—'}</span>
                        {r.overnight ? (
                          <Badge size="xs" variant="light">
                            next day
                          </Badge>
                        ) : null}
                      </Group>
                    ),
                  },
                  {
                    key: 'note',
                    header: 'Note',
                    render: (r) => (
                      <Text size="sm" c="dimmed" lineClamp={1}>
                        {r.note ?? ''}
                      </Text>
                    ),
                  },
                  {
                    key: 'err',
                    header: 'Check',
                    render: (r) =>
                      r.errors.length ? (
                        <Text size="sm" c="red">
                          {r.errors.join(' ')}
                        </Text>
                      ) : (
                        <Badge color="teal" variant="light" size="sm">
                          OK
                        </Badge>
                      ),
                  },
                ]}
              />
            </>
          ) : null}
        </Stack>
      ) : null}

      <Stack gap="sm">
        <SectionTitle>Earlier uploads</SectionTitle>
        <DataTable
          rows={history.data?.uploads ?? []}
          rowKey={(u) => u.id}
          loading={history.loading}
          error={history.error}
          emptyTitle="No uploads yet"
          emptyHint="Uploads you make appear here with who made them and why."
          columns={[
            { key: 'when', header: 'When', render: (u) => relativeTime(u.created_at) },
            { key: 'file', header: 'File', render: (u) => u.filename ?? '—' },
            {
              key: 'reason',
              header: 'Reason',
              render: (u) => (
                <Text size="sm" lineClamp={1}>
                  {u.reason}
                </Text>
              ),
            },
            { key: 'rows', header: 'Rows', align: 'right', render: (u) => <span className="num">{u.rows_total}</span> },
            { key: 'punches', header: 'Punches added', align: 'right', render: (u) => <span className="num">{u.punches}</span> },
            { key: 'by', header: 'By', render: (u) => u.uploaded_by_name ?? '—' },
          ]}
        />
      </Stack>
    </Stack>
  );
}
