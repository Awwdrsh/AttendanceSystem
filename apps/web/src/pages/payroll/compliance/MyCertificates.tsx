import { Button, Group, Paper, Text } from '@mantine/core';
import { IconFileCertificate } from '@tabler/icons-react';
import { compliance } from '../../../api/compliance';

/** Employee: open my tax certificate (for the selected fiscal year) or a salary certificate, as printable pages. */
export function MyCertificates({ fiscalYearStart }: { fiscalYearStart: number }) {
  const open = (kind: 'tax' | 'salary') => window.open(compliance.certificateUrl('me', kind, fiscalYearStart), '_blank', 'noopener');
  return (
    <Paper withBorder p="md">
      <Group justify="space-between" wrap="wrap" gap="sm">
        <div>
          <Text fw={600} size="sm">Certificates</Text>
          <Text size="sm" c="dimmed">Your tax certificate for FY {fiscalYearStart}/{String(fiscalYearStart + 1).slice(-2)}, or a salary certificate for a bank or visa application. Print or save as PDF.</Text>
        </div>
        <Group gap="xs">
          <Button variant="default" size="sm" leftSection={<IconFileCertificate size={16} stroke={1.6} />} onClick={() => open('tax')}>Tax certificate</Button>
          <Button variant="default" size="sm" leftSection={<IconFileCertificate size={16} stroke={1.6} />} onClick={() => open('salary')}>Salary certificate</Button>
        </Group>
      </Group>
    </Paper>
  );
}
