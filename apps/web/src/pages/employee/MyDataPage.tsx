import { Button, Group, List, Paper, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { useState } from 'react';
import { attendance as attApi, biometrics as bioApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { ConfirmDialog } from '../../components/ConfirmDialog';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { addDays, fmtTime, todayNpt } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

/** Transparency page (plan §13.13): what is stored, who sees it, how to withdraw; DSAR export; own punch log. */
export default function MyDataPage() {
  const { user } = useAuth();
  const { t, lang, numerals } = useI18n();
  const employeeId = user?.employeeId ?? '';
  const tr = useQuery(() => bioApi.transparency(), []);
  const consents = useQuery(() => bioApi.consents(employeeId), [employeeId], { enabled: !!employeeId });
  const templates = useQuery(() => bioApi.templates(employeeId), [employeeId], { enabled: !!employeeId });
  const punches = useQuery(() => attApi.punches(employeeId, addDays(todayNpt(), -14), todayNpt()), [employeeId], { enabled: !!employeeId });
  const [revoke, setRevoke] = useState<string | null>(null);
  const l = lang;
  return (
    <Stack>
      <PageHeader title={t('myData')} actions={employeeId ? <Button variant="light" onClick={() => bioApi.dsar(employeeId).catch((e) => toast.err(e))}>{t('exportMyData')}</Button> : null} />
      {tr.data ? (
        <SimpleGrid cols={{ base: 1, md: 2 }} spacing="sm">
          <Paper withBorder p="md" radius="md"><Title order={5}>{t('transparencyTitle')}</Title><List size="sm" mt="xs">{tr.data.stored[l].map((s) => <List.Item key={s}>{s}</List.Item>)}</List>
            <Text size="sm" fw={600} mt="sm">{l === 'ne' ? 'के राखिँदैन' : 'Not stored'}</Text><List size="sm">{tr.data.notStored[l].map((s) => <List.Item key={s}>{s}</List.Item>)}</List></Paper>
          <Paper withBorder p="md" radius="md"><Title order={5}>{l === 'ne' ? 'कसले देख्छ' : 'Who can see it'}</Title><List size="sm" mt="xs">{tr.data.access[l].map((s) => <List.Item key={s}>{s}</List.Item>)}</List>
            <Text size="sm" mt="sm">{tr.data.withdraw[l]}</Text><Text size="xs" c="dimmed" mt="xs">{tr.data.legalBasis[l]}</Text></Paper>
        </SimpleGrid>
      ) : null}
      <Paper withBorder p="md" radius="md">
        <Title order={5}>{l === 'ne' ? 'बायोमेट्रिक सहमति' : 'Biometric consent'}</Title>
        {(consents.data?.consents ?? []).length === 0 ? <Text size="sm" c="dimmed" mt="xs">{l === 'ne' ? 'कुनै सहमति रेकर्ड छैन।' : 'No consent recorded. HR records consent when enrolling you on a terminal.'}</Text> : null}
        <Stack gap="xs" mt="xs">
          {(consents.data?.consents ?? []).map((c) => (
            <Group key={c.id} justify="space-between">
              <Text size="sm">{c.modality} · {c.purpose} · v{c.notice_version} · {new Date(c.granted_at).toLocaleDateString()}{c.revoked_at ? ` · ${l === 'ne' ? 'फिर्ता लिइएको' : 'withdrawn'}` : ''}</Text>
              {!c.revoked_at ? <Button size="xs" variant="light" color="red" onClick={() => setRevoke(c.id)}>{t('withdrawConsent')}</Button> : null}
            </Group>
          ))}
        </Stack>
        <Text size="sm" mt="sm">{l === 'ne' ? 'भण्डारण गरिएका टेम्प्लेट' : 'Stored templates'}: {(templates.data?.templates ?? []).filter((x) => !x.shredded_at).length}</Text>
      </Paper>
      <Paper withBorder p="md" radius="md">
        <Title order={5}>{t('rawPunches')} · {l === 'ne' ? 'पछिल्लो १४ दिन' : 'last 14 days'}</Title>
        <Stack gap={2} mt="xs">
          {(punches.data?.punches ?? []).length === 0 ? <Text size="sm" c="dimmed">{t('nothingHere')}</Text> : null}
          {(punches.data?.punches ?? []).map((p) => <Text key={p.id} size="sm" c={p.superseded ? 'dimmed' : undefined}>{p.local_date} {fmtTime(p.punched_at, numerals)} · {p.direction.toUpperCase()} · {p.verify_mode} · {p.source}{p.device_sn ? ` · ${p.device_sn}` : ''}</Text>)}
        </Stack>
      </Paper>
      <ConfirmDialog opened={!!revoke} onClose={() => setRevoke(null)} title={t('withdrawConsent')} confirmLabel={t('withdrawConsent')}
        message={l === 'ne' ? 'तपाईंका टेम्प्लेट तुरुन्त नष्ट गरिनेछ। त्यसपछि कार्ड वा पिनबाट हाजिरी गर्नुहोस्।' : 'Your templates for this modality will be shredded immediately. You will punch with a card or PIN afterwards.'}
        onConfirm={async () => { if (!revoke) return; try { const r = await bioApi.revoke(revoke); toast.ok(`Consent withdrawn; ${r.templatesShredded} template(s) shredded.`); await Promise.all([consents.reload(), templates.reload()]); } catch (e) { toast.err(e); } }} />
    </Stack>
  );
}
