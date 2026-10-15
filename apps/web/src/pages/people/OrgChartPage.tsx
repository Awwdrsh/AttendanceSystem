import { ActionIcon, Badge, Box, Button, Group, Paper, Skeleton, Stack, Text, TextInput, UnstyledButton } from '@mantine/core';
import { useDebouncedValue } from '@mantine/hooks';
import { IconChevronDown, IconChevronRight, IconSearch, IconSitemap } from '@tabler/icons-react';
import { useMemo, useState } from 'react';
import { people, type OrgNode } from '../../api/people';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { PersonAvatar } from '../../components/people/PersonAvatar';
import { PersonCardModal } from '../../components/people/PersonCardModal';
import { useQuery } from '../../lib/useQuery';

interface Tree { roots: OrgNode[]; children: Map<string, OrgNode[]>; parent: Map<string, string>; size: Map<string, number> }

/** Build the reporting tree; anyone whose manager is missing (or would form a loop) becomes a root. */
function buildTree(nodes: OrgNode[]): Tree {
  const byId = new Map(nodes.map((n) => [n.id, n]));
  const parent = new Map<string, string>();
  for (const n of nodes) if (n.manager_id && byId.has(n.manager_id) && n.manager_id !== n.id) parent.set(n.id, n.manager_id);
  // Break loops: walk up from each node; a node that reaches itself loses its parent link.
  for (const n of nodes) {
    const seen = new Set<string>([n.id]); let cur = parent.get(n.id);
    while (cur) { if (seen.has(cur)) { parent.delete(n.id); break; } seen.add(cur); cur = parent.get(cur); }
  }
  const children = new Map<string, OrgNode[]>();
  const roots: OrgNode[] = [];
  for (const n of nodes) {
    const p = parent.get(n.id);
    if (p) children.set(p, [...(children.get(p) ?? []), n]); else roots.push(n);
  }
  const size = new Map<string, number>();
  const count = (id: string): number => {
    if (size.has(id)) return size.get(id)!;
    const s = (children.get(id) ?? []).reduce((acc, c) => acc + 1 + count(c.id), 0);
    size.set(id, s); return s;
  };
  for (const n of nodes) count(n.id);
  // Managers with the biggest teams first, so the chart reads top-down.
  roots.sort((a, b) => (size.get(b.id) ?? 0) - (size.get(a.id) ?? 0) || a.name_en.localeCompare(b.name_en));
  return { roots, children, parent, size };
}

function NodeRow({ node, tree, depth, expanded, toggle, highlight, onOpen }: {
  node: OrgNode; tree: Tree; depth: number; expanded: Set<string>; toggle: (id: string) => void; highlight: Set<string>; onOpen: (id: string) => void;
}) {
  const kids = tree.children.get(node.id) ?? [];
  const open = expanded.has(node.id);
  const hit = highlight.has(node.id);
  return (
    <Box>
      <Group gap={6} wrap="nowrap" py={4} pl={depth * 22} style={{ borderRadius: 6, background: hit ? 'var(--hj-hydro-soft)' : undefined }}>
        {kids.length ? (
          <ActionIcon variant="subtle" color="gray" size="sm" onClick={() => toggle(node.id)} aria-label={open ? `Collapse ${node.name_en}` : `Expand ${node.name_en}`}>
            {open ? <IconChevronDown size={16} stroke={1.6} /> : <IconChevronRight size={16} stroke={1.6} />}
          </ActionIcon>
        ) : <Box w={22} />}
        <UnstyledButton onClick={() => onOpen(node.id)} style={{ flex: 1, minWidth: 0 }}>
          <Group gap="sm" wrap="nowrap">
            <PersonAvatar id={node.id} name={node.name_en} hasAvatar={node.has_avatar} size={32} />
            <div style={{ minWidth: 0 }}>
              <Text size="sm" fw={hit ? 700 : 600} truncate>{node.name_en}</Text>
              <Text size="xs" c="dimmed" truncate>{[node.designation, node.unit_name].filter(Boolean).join(', ') || '—'}</Text>
            </div>
            {kids.length ? <Badge variant="light" color="gray" size="sm" ml="auto" style={{ flexShrink: 0 }}>{tree.size.get(node.id)} {tree.size.get(node.id) === 1 ? 'report' : 'in team'}</Badge> : null}
          </Group>
        </UnstyledButton>
      </Group>
      {open && kids.length ? (
        <Box style={{ borderLeft: '1px solid var(--hj-line)', marginLeft: depth * 22 + 10 }}>
          {kids.map((k) => <NodeRow key={k.id} node={k} tree={tree} depth={1} expanded={expanded} toggle={toggle} highlight={highlight} onOpen={onOpen} />)}
        </Box>
      ) : null}
    </Box>
  );
}

/** Who reports to whom, from the current manager of each person. */
export default function OrgChartPage() {
  const q = useQuery(() => people.orgChart(), []);
  const tree = useMemo(() => buildTree(q.data?.nodes ?? []), [q.data]);
  const [expanded, setExpanded] = useState<Set<string> | null>(null);
  const [search, setSearch] = useState('');
  const [debounced] = useDebouncedValue(search, 200);
  const [open, setOpen] = useState<string | null>(null);

  // Start with the top two levels open.
  const defaults = useMemo(() => new Set(tree.roots.filter((r) => tree.children.has(r.id)).map((r) => r.id)), [tree]);
  const highlight = useMemo(() => {
    const s = debounced.trim().toLowerCase();
    if (!s) return new Set<string>();
    return new Set((q.data?.nodes ?? []).filter((n) => n.name_en.toLowerCase().includes(s) || (n.designation ?? '').toLowerCase().includes(s)).map((n) => n.id));
  }, [debounced, q.data]);
  // A search opens the path down to every match.
  const shown = useMemo(() => {
    const base = new Set(expanded ?? defaults);
    for (const id of highlight) { let p = tree.parent.get(id); while (p) { base.add(p); p = tree.parent.get(p); } }
    return base;
  }, [expanded, defaults, highlight, tree]);
  const toggle = (id: string) => setExpanded(() => { const n = new Set(shown); if (n.has(id)) n.delete(id); else n.add(id); return n; });
  const all = () => setExpanded(new Set([...tree.children.keys()]));

  return (
    <Stack gap="lg">
      <PageHeader title="Org chart" subtitle="Reporting lines across the company. Select anyone to see their card."
        actions={<Group gap="xs"><Button variant="default" size="xs" onClick={all}>Expand all</Button><Button variant="default" size="xs" onClick={() => setExpanded(new Set())}>Collapse all</Button></Group>} />
      <TextInput placeholder="Find someone by name or role" leftSection={<IconSearch size={16} stroke={1.6} />} value={search} onChange={(e) => setSearch(e.currentTarget.value)} maw={420} aria-label="Find someone" />
      {q.loading && !q.data ? <Stack gap="xs">{Array.from({ length: 6 }, (_, i) => <Skeleton key={i} h={40} />)}</Stack>
        : q.error ? <EmptyState title="We could not load the org chart" hint={q.error} action={{ label: 'Try again', onClick: () => void q.reload() }} />
          : !tree.roots.length ? <EmptyState icon={<IconSitemap size={20} stroke={1.6} />} title="No reporting lines yet" hint="Set each employee's manager on their record and the chart builds itself." />
            : (
              <Paper withBorder p="sm">
                {debounced.trim() && !highlight.size ? <Text size="sm" c="dimmed" p="xs">Nobody matches “{debounced.trim()}”.</Text> : null}
                {tree.roots.map((r) => <NodeRow key={r.id} node={r} tree={tree} depth={0} expanded={shown} toggle={toggle} highlight={highlight} onOpen={setOpen} />)}
              </Paper>
            )}
      <PersonCardModal id={open} onClose={() => setOpen(null)} />
    </Stack>
  );
}
