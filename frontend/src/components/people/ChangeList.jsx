import { Stack, Table, Text } from '@mantine/core';
/** Field-by-field "from → to" of a profile change. */
export function ChangeList({ changes }) {
  if (!changes.length)
    return (
      <Text size="sm" c="dimmed">
        No differences.
      </Text>
    );
  return (
    <Table withRowBorders={false} verticalSpacing={4} horizontalSpacing="xs">
      <Table.Tbody>
        {changes.map((c) => (
          <Table.Tr key={c.field}>
            <Table.Td w="32%" style={{ verticalAlign: 'top' }}>
              <Text size="sm" c="dimmed">
                {c.label}
              </Text>
            </Table.Td>
            <Table.Td>
              <Stack gap={0}>
                <Text size="sm" td={c.from ? 'line-through' : undefined} c="dimmed">
                  {c.from ?? '(empty)'}
                </Text>
                <Text size="sm" fw={500}>
                  {c.to ?? '(cleared)'}
                </Text>
              </Stack>
            </Table.Td>
          </Table.Tr>
        ))}
      </Table.Tbody>
    </Table>
  );
}
