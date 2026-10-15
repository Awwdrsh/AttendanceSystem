import { Badge, Button, Group, Modal, Paper, PasswordInput, Stack, Text, TextInput } from '@mantine/core';
import { IconDeviceDesktop, IconDeviceMobile, IconLogout, IconShieldCheck } from '@tabler/icons-react';
import { useState } from 'react';
import { security, type SessionInfo } from '../../api/security';
import { useAuth } from '../../auth/AuthProvider';
import { DataTable } from '../../components/DataTable';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { RecoveryCodes, TwoFactorSetup } from '../../components/TwoFactorSetup';
import { fmtAd, fmtTime, NPT_OFFSET_MIN, relativeTime } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const stamp = (iso: string) => `${fmtAd(new Date(Date.parse(iso) + NPT_OFFSET_MIN * 60_000).toISOString().slice(0, 10))}, ${fmtTime(iso)}`;

/** Two-step sign-in and the places you are signed in. */
export default function SecurityPage() {
  const { refresh } = useAuth();
  const st = useQuery(() => security.status(), []);
  const sessions = useQuery(() => security.sessions(), []);
  const [setupOpen, setSetupOpen] = useState(false);
  const [codesOpen, setCodesOpen] = useState(false);
  const [offOpen, setOffOpen] = useState(false);
  const [code, setCode] = useState(''); const [password, setPassword] = useState('');
  const [newCodes, setNewCodes] = useState<string[] | null>(null);
  const [busy, setBusy] = useState(false);
  const s = st.data;

  const regenerate = async () => {
    setBusy(true);
    try { setNewCodes((await security.recoveryCodes(code.trim())).recoveryCodes); setCode(''); } catch (e) { toast.err(e, 'Not changed'); } finally { setBusy(false); }
  };
  const turnOff = async () => {
    setBusy(true);
    try { await security.disable(password, code.trim()); toast.ok('Two-step sign-in is off.'); setOffOpen(false); setCode(''); setPassword(''); await Promise.all([st.reload(), refresh()]); }
    catch (e) { toast.err(e, 'Still on'); } finally { setBusy(false); }
  };
  const signOut = async (x: SessionInfo) => {
    try { await security.revokeSession(x.id); toast.ok(`Signed out ${x.device}.`); await sessions.reload(); } catch (e) { toast.err(e, 'Not signed out'); }
  };
  const signOutOthers = async () => {
    try { const r = await security.revokeOthers(); toast.ok(r.revoked ? `Signed out ${r.revoked} other ${r.revoked === 1 ? 'session' : 'sessions'}.` : 'No other sessions.'); await sessions.reload(); } catch (e) { toast.err(e); }
  };
  const others = (sessions.data?.sessions ?? []).filter((x) => !x.current).length;

  return (
    <Stack gap="lg">
      <PageHeader title="Sign-in security" subtitle="A second step after your password, and every place your account is signed in." />

      <Paper withBorder p="lg">
        <SectionTitle hint="After your password, Hajiri asks for a code from an authenticator app on your phone."
          actions={s ? (s.enabled ? <Badge color="teal" variant="light" leftSection={<IconShieldCheck size={12} />}>On</Badge> : <Badge color="gray" variant="light">Off</Badge>) : null}>
          Two-step sign-in
        </SectionTitle>
        {s?.enabled ? (
          <Stack gap="sm" mt="sm">
            <Text size="sm">Turned on {s.enabledAt ? stamp(s.enabledAt) : ''}. You have <Text span fw={600} className="num">{s.recoveryCodesLeft}</Text> unused recovery {s.recoveryCodesLeft === 1 ? 'code' : 'codes'}.</Text>
            {s.recoveryCodesLeft <= 3 ? <Text size="sm" c="orange">You are running low on recovery codes. Make new ones and save them.</Text> : null}
            {s.required ? <Text size="sm" c="dimmed">Your company requires two-step sign-in for your role, so it cannot be turned off.</Text> : null}
            <Group gap="xs">
              <Button variant="default" size="sm" onClick={() => { setNewCodes(null); setCode(''); setCodesOpen(true); }}>New recovery codes</Button>
              {!s.required ? <Button variant="subtle" color="red" size="sm" onClick={() => { setCode(''); setPassword(''); setOffOpen(true); }}>Turn off</Button> : null}
            </Group>
          </Stack>
        ) : s ? (
          <Stack gap="sm" mt="sm">
            <Text size="sm" c="dimmed">Recommended for everyone, and especially for HR and managers, who can see other people's pay and records.</Text>
            <Group><Button onClick={() => setSetupOpen(true)} leftSection={<IconShieldCheck size={16} stroke={1.6} />}>Turn on two-step sign-in</Button></Group>
          </Stack>
        ) : null}
      </Paper>

      <div>
        <SectionTitle hint="Sessions end after an hour without use and always after 12 hours." actions={others > 0 ? <Button size="xs" variant="default" leftSection={<IconLogout size={14} />} onClick={signOutOthers}>Sign out everywhere else</Button> : null}>
          Where you're signed in
        </SectionTitle>
        <DataTable<SessionInfo> rows={sessions.data?.sessions ?? []} rowKey={(x) => x.id} loading={sessions.loading} error={sessions.error} emptyTitle="No active sessions"
          columns={[
            { key: 'device', header: 'Device', render: (x) => (
              <Group gap="xs" wrap="nowrap">
                {/Android|iOS/.test(x.device) ? <IconDeviceMobile size={18} stroke={1.6} /> : <IconDeviceDesktop size={18} stroke={1.6} />}
                <div>
                  <Group gap={6} wrap="nowrap"><Text size="sm" fw={500}>{x.device}</Text>{x.current ? <Badge size="xs" variant="light">This device</Badge> : null}</Group>
                  <Text size="xs" c="dimmed" className="mono">{x.ip ?? 'Unknown address'}</Text>
                </div>
              </Group>) },
            { key: 'since', header: 'Signed in', render: (x) => <Text size="sm">{stamp(x.createdAt)}</Text> },
            { key: 'seen', header: 'Last active', render: (x) => <Text size="sm">{relativeTime(x.lastSeenAt)}</Text> },
            { key: 'two', header: 'Two-step', render: (x) => x.twoFactor ? <Badge size="sm" color="teal" variant="light">Verified</Badge> : <Text size="sm" c="dimmed">Password only</Text> },
            { key: 'act', header: '', align: 'right', render: (x) => x.current ? null : <Button size="xs" variant="subtle" color="red" onClick={() => void signOut(x)}>Sign out</Button> },
          ]} />
      </div>

      <Modal opened={setupOpen} onClose={() => setSetupOpen(false)} title={<Text fw={600}>Turn on two-step sign-in</Text>} size="lg" centered>
        <TwoFactorSetup onEnabled={async () => { setSetupOpen(false); toast.ok('Two-step sign-in is on.'); await Promise.all([st.reload(), sessions.reload(), refresh()]); }} />
      </Modal>

      <Modal opened={codesOpen} onClose={() => setCodesOpen(false)} title={<Text fw={600}>New recovery codes</Text>} centered>
        {newCodes ? <RecoveryCodes codes={newCodes} onDone={() => { setCodesOpen(false); void st.reload(); }} doneLabel="Done" /> : (
          <Stack>
            <Text size="sm" c="dimmed">Your old recovery codes stop working. Type the current code from your authenticator app to continue.</Text>
            <TextInput label="Code from the app" inputMode="numeric" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.currentTarget.value)} className="mono" />
            <Group justify="flex-end"><Button loading={busy} disabled={code.trim().length < 6} onClick={regenerate}>Make new codes</Button></Group>
          </Stack>
        )}
      </Modal>

      <Modal opened={offOpen} onClose={() => setOffOpen(false)} title={<Text fw={600}>Turn off two-step sign-in</Text>} centered>
        <Stack>
          <Text size="sm" c="dimmed">Your password alone will sign you in again. Confirm with your password and a current code (or a recovery code).</Text>
          <PasswordInput label="Password" autoComplete="current-password" value={password} onChange={(e) => setPassword(e.currentTarget.value)} />
          <TextInput label="Code" autoComplete="one-time-code" value={code} onChange={(e) => setCode(e.currentTarget.value)} className="mono" />
          <Group justify="flex-end"><Button color="red" loading={busy} disabled={!password || code.trim().length < 6} onClick={turnOff}>Turn off</Button></Group>
        </Stack>
      </Modal>
    </Stack>
  );
}
