import { Button, FileButton, Group, Text } from '@mantine/core';
import { IconPaperclip, IconX } from '@tabler/icons-react';
import { useState } from 'react';
import { FILE_ACCEPT, uploadFile, type UploadedFile, type UploadPurpose } from '../api/files';
import { toast } from '../lib/toast';

/** Pick a PDF or image, upload it straight away, and hand the stored file back to the form. */
export function FileUpload({ purpose, value, onChange, label = 'Attach file', accept = FILE_ACCEPT }: {
  purpose: UploadPurpose; value: UploadedFile | null; onChange: (f: UploadedFile | null) => void; label?: string; accept?: string;
}) {
  const [busy, setBusy] = useState(false);
  if (value) {
    return (
      <Group gap="xs" wrap="nowrap">
        <IconPaperclip size={16} stroke={1.6} />
        <Text size="sm" truncate maw={260}>{value.filename}</Text>
        <Button variant="subtle" size="compact-xs" color="gray" onClick={() => onChange(null)} leftSection={<IconX size={12} />}>Remove</Button>
      </Group>
    );
  }
  return (
    <FileButton accept={accept} onChange={async (f) => {
      if (!f) return;
      setBusy(true);
      try { onChange(await uploadFile(f, purpose)); } catch (e) { toast.err(e, 'Upload failed'); } finally { setBusy(false); }
    }}>
      {(props) => <Button {...props} variant="default" size="sm" loading={busy} leftSection={<IconPaperclip size={16} stroke={1.6} />}>{label}</Button>}
    </FileButton>
  );
}
