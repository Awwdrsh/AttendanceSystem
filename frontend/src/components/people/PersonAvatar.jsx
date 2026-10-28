import { Avatar } from '@mantine/core';
import { avatarUrl } from '../../api/people';

/** A person's photo when they have one, otherwise their initials on the brand tint. */
export function PersonAvatar({ id, name, hasAvatar, size = 40, version, ...rest }) {
  const src = hasAvatar ? `${avatarUrl(id)}${version ? `?v=${version}` : ''}` : null;
  return <Avatar src={src} name={name} alt={name} size={size} radius="xl" color="brand" variant="light" {...rest} />;
}
