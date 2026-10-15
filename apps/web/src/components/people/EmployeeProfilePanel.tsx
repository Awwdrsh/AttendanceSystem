import { Paper, Stack } from '@mantine/core';
import { people } from '../../api/people';
import { useQuery } from '../../lib/useQuery';
import { AvatarEditor } from './AvatarEditor';
import { ProfileSections } from './ProfileSections';

/** HR's view of the full employee profile: photo plus every profile section, editable. */
export function EmployeeProfilePanel({ employeeId }: { employeeId: string }) {
  const card = useQuery(() => people.card(employeeId), [employeeId]);
  const p = card.data?.person;
  return (
    <Stack gap="md">
      {p ? <Paper withBorder p="md"><AvatarEditor id={p.id} name={p.name_en} hasAvatar={p.has_avatar} self={false} onChanged={() => void card.reload()} /></Paper> : null}
      <ProfileSections employeeId={employeeId} mode="hr" />
    </Stack>
  );
}
