import { Alert, Button, Code, CopyButton, Group, Paper, SimpleGrid, Stack, Text, TextInput } from '@mantine/core';
import { IconCheck, IconCopy, IconDownload, IconShieldLock } from '@tabler/icons-react';
import { useState } from 'react';
import { security } from '../api/security';
import { errorMessage } from '../api/client';
import { QrCode } from './QrCode';

/** Recovery codes, shown once: copy or download, then confirm they are saved. */
export function RecoveryCodes({ codes, onDone, doneLabel = 'I have saved these codes' }) {
  const text = codes.join('\n');
  const download = () => {
    const url = URL.createObjectURL(
      new Blob([`Hajiri recovery codes\nEach code works once. Keep them somewhere safe.\n\n${text}\n`], { type: 'text/plain' }),
    );
    const a = document.createElement('a');
    a.href = url;
    a.download = 'hajiri-recovery-codes.txt';
    a.click();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
  };
  return (
    <Stack>
      <Alert color="orange" variant="light" title="Save your recovery codes now">
        If you lose your phone, each of these codes signs you in once. They are not shown again.
      </Alert>
      <Paper withBorder p="md">
        <SimpleGrid cols={2} spacing="xs">
          {codes.map((c) => (
            <Text key={c} className="mono" size="sm" ta="center">
              {c}
            </Text>
          ))}
        </SimpleGrid>
      </Paper>
      <Group justify="space-between" wrap="wrap">
        <Group gap="xs">
          <CopyButton value={text}>
            {({ copied, copy }) => (
              <Button variant="default" size="sm" leftSection={copied ? <IconCheck size={16} /> : <IconCopy size={16} />} onClick={copy}>
                {copied ? 'Copied' : 'Copy'}
              </Button>
            )}
          </CopyButton>
          <Button variant="default" size="sm" leftSection={<IconDownload size={16} />} onClick={download}>
            Download
          </Button>
        </Group>
        <Button onClick={onDone}>{doneLabel}</Button>
      </Group>
    </Stack>
  );
}

/** Scan the QR code (or type the key), confirm with a code, then save the recovery codes. */
export function TwoFactorSetup({ onEnabled }) {
  const [secret, setSecret] = useState(null);
  const [code, setCode] = useState('');
  const [codes, setCodes] = useState(null);
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const start = async () => {
    setBusy(true);
    setError(null);
    try {
      setSecret(await security.setup());
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };
  const confirm = async () => {
    setBusy(true);
    setError(null);
    try {
      setCodes((await security.enable(code.replace(/\s/g, ''))).recoveryCodes);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setBusy(false);
    }
  };

  if (codes) return <RecoveryCodes codes={codes} onDone={onEnabled} />;
  if (!secret) {
    return (
      <Stack gap="sm">
        <Text size="sm" c="dimmed">
          You need an authenticator app on your phone, such as Google Authenticator, Microsoft Authenticator or 2FAS. After your password,
          Hajiri will ask for the 6-digit code the app shows.
        </Text>
        {error ? (
          <Alert color="red" variant="light">
            {error}
          </Alert>
        ) : null}
        <Group>
          <Button leftSection={<IconShieldLock size={16} stroke={1.6} />} loading={busy} onClick={start}>
            Set up two-step sign-in
          </Button>
        </Group>
      </Stack>
    );
  }
  // Show the key in groups of four so it is easy to type.
  const grouped = secret.secret.replace(/(.{4})/g, '$1 ').trim();
  return (
    <Stack>
      <Group align="flex-start" gap="xl" wrap="wrap">
        <QrCode value={secret.otpauthUri} size={188} label="QR code for your authenticator app" />
        <Stack gap={6} style={{ flex: 1, minWidth: 220 }}>
          <Text size="sm" fw={600}>
            1. Scan this code with your authenticator app
          </Text>
          <Text size="sm" c="dimmed">
            Can't scan? Choose "enter a setup key" in the app and type:
          </Text>
          <Group gap="xs" wrap="nowrap">
            <Code className="mono" style={{ wordBreak: 'break-all' }}>
              {grouped}
            </Code>
            <CopyButton value={secret.secret}>
              {({ copied, copy }) => (
                <Button size="compact-xs" variant="subtle" onClick={copy}>
                  {copied ? 'Copied' : 'Copy'}
                </Button>
              )}
            </CopyButton>
          </Group>
          <Text size="sm" fw={600} mt="sm">
            2. Type the 6-digit code the app shows
          </Text>
          <Group gap="xs" align="flex-end" wrap="nowrap">
            <TextInput
              aria-label="Code from the app"
              placeholder="123 456"
              inputMode="numeric"
              autoComplete="one-time-code"
              maxLength={7}
              value={code}
              onChange={(e) => setCode(e.currentTarget.value.replace(/[^\d ]/g, ''))}
              className="mono"
              w={140}
              onKeyDown={(e) => {
                if (e.key === 'Enter' && code.replace(/\s/g, '').length === 6) void confirm();
              }}
            />
            <Button loading={busy} disabled={code.replace(/\s/g, '').length !== 6} onClick={confirm}>
              Turn on
            </Button>
          </Group>
        </Stack>
      </Group>
      {error ? (
        <Alert color="red" variant="light">
          {error}
        </Alert>
      ) : null}
    </Stack>
  );
}
