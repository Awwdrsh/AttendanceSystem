import { Button, FileButton, Group } from '@mantine/core';
import { IconCamera, IconTrash } from '@tabler/icons-react';
import { useState } from 'react';
import { uploadFile } from '../../api/files';
import { people } from '../../api/people';
import { toast } from '../../lib/toast';
import { PersonAvatar } from './PersonAvatar';

/** Shrink a picked photo to a 320 px square JPEG in the browser, so profile photos stay small. */
async function squareJpeg(file: File, size = 320): Promise<File> {
  const url = URL.createObjectURL(file);
  try {
    const img = await new Promise<HTMLImageElement>((resolve, reject) => {
      const i = new Image(); i.onload = () => resolve(i); i.onerror = () => reject(new Error('That file is not a photo we can read.')); i.src = url;
    });
    const side = Math.min(img.naturalWidth, img.naturalHeight);
    const canvas = document.createElement('canvas');
    canvas.width = size; canvas.height = size;
    canvas.getContext('2d')!.drawImage(img, (img.naturalWidth - side) / 2, (img.naturalHeight - side) / 2, side, side, 0, 0, size, size);
    const blob = await new Promise<Blob>((resolve, reject) => canvas.toBlob((b) => (b ? resolve(b) : reject(new Error('The photo could not be processed.'))), 'image/jpeg', 0.85));
    return new File([blob], 'profile.jpg', { type: 'image/jpeg' });
  } finally { URL.revokeObjectURL(url); }
}

/** Profile photo with change and remove; `employeeId` set means HR is editing someone else's photo. */
export function AvatarEditor({ id, name, hasAvatar, self, onChanged }: { id: string; name: string; hasAvatar: boolean; self: boolean; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [version, setVersion] = useState(0);
  const change = async (f: File | null) => {
    if (!f) return;
    setBusy(true);
    try {
      const up = await uploadFile(await squareJpeg(f), 'avatar');
      if (self) await people.setMyAvatar(up.id); else await people.setAvatar(id, up.id);
      setVersion(Date.now()); toast.ok('Photo updated.'); onChanged();
    } catch (e) { toast.err(e, 'Photo not saved'); } finally { setBusy(false); }
  };
  const remove = async () => {
    setBusy(true);
    try { if (self) await people.removeMyAvatar(); else await people.removeAvatar(id); toast.ok('Photo removed.'); onChanged(); } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Group gap="md" wrap="nowrap">
      <PersonAvatar id={id} name={name} hasAvatar={hasAvatar} size={64} version={version} />
      <Group gap="xs">
        <FileButton accept="image/jpeg,image/png,image/webp" onChange={change}>
          {(props) => <Button {...props} size="xs" variant="default" loading={busy} leftSection={<IconCamera size={14} stroke={1.6} />}>{hasAvatar ? 'Change photo' : 'Add a photo'}</Button>}
        </FileButton>
        {hasAvatar ? <Button size="xs" variant="subtle" color="red" disabled={busy} onClick={remove} leftSection={<IconTrash size={14} stroke={1.6} />}>Remove</Button> : null}
      </Group>
    </Group>
  );
}
