import { Button, Stack } from '@mantine/core';
import { IconFilePlus } from '@tabler/icons-react';
import { useState } from 'react';
import { documents as docsApi } from '../../api/documents';
import { SectionTitle } from '../../components/PageHeader';
import { useQuery } from '../../lib/useQuery';
import { EmployeeDocuments } from './EmployeeDocuments';
import { IssueLetterDrawer, LetterTable } from './Letters';

/** Documents tab on an employee's page: their documents with verification and history, and the letters issued to them. */
export function EmployeeDocumentsTab({ employeeId }: { employeeId: string }) {
  const letters = useQuery(() => docsApi.letters({ employeeId }), [employeeId]);
  const [issuing, setIssuing] = useState(false);
  return (
    <Stack gap="xl">
      <EmployeeDocuments employeeId={employeeId} mode="hr" />
      <div>
        <SectionTitle actions={<Button size="xs" variant="light" leftSection={<IconFilePlus size={14} stroke={1.6} />} onClick={() => setIssuing(true)}>Issue a letter</Button>}>Letters</SectionTitle>
        <LetterTable letters={letters.data?.letters ?? []} loading={letters.loading} error={letters.error} hr onChanged={() => { void letters.reload(); }} />
      </div>
      <IssueLetterDrawer opened={issuing} onClose={() => setIssuing(false)} employeeId={employeeId} onIssued={() => { void letters.reload(); }} />
    </Stack>
  );
}
