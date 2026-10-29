import { Badge, Group, Loader, Menu, Text, UnstyledButton } from '@mantine/core';
import { IconBuildingSkyscraper, IconCheck, IconSelector } from '@tabler/icons-react';
import { useState } from 'react';
import { useCompanies } from '../auth/CompaniesProvider';
import { ROLE_LABEL } from '../lib/access';
import { toast } from '../lib/toast';

const roleList = (roles) => roles.map((r) => ROLE_LABEL[r] ?? r).join(', ');

/**
 * The company this session works in, and the others the person can switch to. Shown only to people with more than
 * one company. `compact` is the narrow form for the mobile header.
 */
export function CompanySwitcher({ compact }) {
  const { me, active, switchTo } = useCompanies();
  const [busy, setBusy] = useState(null);
  const companies = me?.companies ?? [];
  if (companies.length < 2 || !active) return null;

  const choose = async (c) => {
    if (c.active || busy) return;
    setBusy(c.id);
    try {
      await switchTo(c.id);
    } catch (e) {
      toast.err(e, 'Could not switch company');
      setBusy(null);
    }
  };

  return (
    <Menu position={compact ? 'bottom-end' : 'bottom-start'} width={280} shadow="md" offset={6}>
      <Menu.Target>
        <UnstyledButton
          className="hj-company-switch"
          aria-label={`Working in ${active.name}. Switch company`}
          p={compact ? 4 : 8}
          w={compact ? undefined : '100%'}
          style={{ borderRadius: 8, border: compact ? undefined : '1px solid var(--hj-line)', maxWidth: compact ? 180 : undefined }}
        >
          <Group gap={8} wrap="nowrap">
            {busy ? <Loader size={16} /> : <IconBuildingSkyscraper size={17} stroke={1.6} color="var(--hj-muted)" />}
            <div style={{ minWidth: 0, flex: 1 }}>
              <Text size="sm" fw={500} truncate lh={1.25}>
                {active.name}
              </Text>
              {compact ? null : (
                <Text size="xs" c="dimmed" truncate lh={1.25}>
                  {active.code}
                  {me?.groupName ? ` · ${me.groupName}` : ''}
                </Text>
              )}
            </div>
            <IconSelector size={15} color="var(--hj-muted)" />
          </Group>
        </UnstyledButton>
      </Menu.Target>
      <Menu.Dropdown>
        <Menu.Label>Switch company</Menu.Label>
        {companies.map((c) => (
          <Menu.Item
            key={c.id}
            onClick={() => void choose(c)}
            disabled={!!busy && busy !== c.id}
            leftSection={c.active ? <IconCheck size={15} stroke={2} /> : <span style={{ width: 15, display: 'inline-block' }} />}
            rightSection={
              c.home ? (
                <Badge size="xs" variant="light" color="gray">
                  Home
                </Badge>
              ) : null
            }
          >
            <Text size="sm" fw={c.active ? 600 : 500} truncate>
              {c.name}
            </Text>
            <Text size="xs" c="dimmed" truncate>
              {c.code}
              {c.roles.length ? ` · ${roleList(c.roles)}` : ''}
            </Text>
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}
