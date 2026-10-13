import { Button, Group, Paper, SegmentedControl, SimpleGrid, Stack, Text, Title } from '@mantine/core';
import { Link } from 'react-router-dom';
import { employees as empApi } from '../../api';
import { useAuth } from '../../auth/AuthProvider';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { dualDate } from '../../lib/format';
import { useQuery } from '../../lib/useQuery';

export default function ProfilePage() {
  const { user } = useAuth();
  const { t, lang, setLang, numerals, setNumerals } = useI18n();
  const emp = useQuery(() => empApi.get(user!.employeeId!), [user?.employeeId], { enabled: !!user?.employeeId });
  const e = emp.data?.employee;
  const row = (k: string, v: string | null | undefined) => <div><Text size="xs" c="dimmed">{k}</Text><Text>{v || '—'}</Text></div>;
  return (
    <Stack>
      <PageHeader title={t('profile')} actions={<Button component={Link} to="/change-password" variant="light">{t('changePassword')}</Button>} />
      <Paper withBorder p="md" radius="md">
        <Title order={5} mb="sm">{user?.name}</Title>
        <SimpleGrid cols={{ base: 2, md: 4 }}>
          {row(t('email'), user?.email)}
          {row('Roles', user?.roles.join(', '))}
          {e ? row('Employee code', e.emp_code) : null}
          {e ? row('नाम', e.name_ne) : null}
          {e ? row('Designation', e.designation) : null}
          {e ? row('Joined', e.join_date ? `${dualDate(e.join_date, lang, numerals).bs} · ${dualDate(e.join_date).ad}` : null) : null}
          {e ? row('Regime', e.regime.toUpperCase()) : null}
          {e ? row('Bank', e.bank_code ? `${e.bank_code} ****${e.bank_account_last4 ?? ''}` : null) : null}
        </SimpleGrid>
      </Paper>
      <Paper withBorder p="md" radius="md">
        <Group gap="xl" wrap="wrap">
          <div><Text size="sm" fw={600}>{t('language')}</Text><SegmentedControl value={lang} onChange={(v) => setLang(v as 'en' | 'ne')} data={[{ value: 'ne', label: 'नेपाली' }, { value: 'en', label: 'English' }]} /></div>
          <div><Text size="sm" fw={600}>{t('numerals')}</Text><SegmentedControl value={numerals} onChange={(v) => setNumerals(v as 'latin' | 'devanagari')} data={[{ value: 'latin', label: '0-9' }, { value: 'devanagari', label: '०-९' }]} /></div>
        </Group>
        <Text size="xs" c="dimmed" mt="sm">IDs, PAN, bank accounts and phone numbers are always shown in Latin digits.</Text>
      </Paper>
    </Stack>
  );
}
