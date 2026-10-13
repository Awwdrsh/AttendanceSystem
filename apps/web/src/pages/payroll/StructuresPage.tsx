import { Badge, Button, Group, Modal, MultiSelect, Select, SimpleGrid, Stack, Switch, Tabs, Text, TextInput } from '@mantine/core';
import { useState } from 'react';
import { payroll as payApi } from '../../api';
import type { SalaryComponent, SalaryStructure } from '../../api/types';
import { DataTable } from '../../components/DataTable';
import { PageHeader } from '../../components/PageHeader';
import { useI18n } from '../../i18n';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

export default function StructuresPage() {
  const { t } = useI18n();
  const comps = useQuery(() => payApi.components(), []);
  const structs = useQuery(() => payApi.structures(), []);
  const [edit, setEdit] = useState<Record<string, unknown> | null>(null); const [busy, setBusy] = useState(false);
  const [newS, setNewS] = useState<{ code: string; nameEn: string; components: string[] } | null>(null);
  const set = (k: string, v: unknown) => setEdit((e) => e && ({ ...e, [k]: v }));
  const save = async () => {
    if (!edit) return; setBusy(true);
    try {
      const { isNew, statutory: _s, ...b } = edit as { isNew?: boolean; statutory?: boolean; code: string } & Record<string, unknown>;
      if (isNew) await payApi.createComponent(b); else { const { code, ...rest } = b; await payApi.updateComponent(code, rest); }
      toast.ok('Component saved.'); setEdit(null); await comps.reload();
    } catch (e) { toast.err(e); } finally { setBusy(false); }
  };
  return (
    <Stack>
      <PageHeader title={t('structures')} subtitle="Components carry a formula in a small, safe expression language (no code). Statutory components are locked. Structures freeze once assigned." />
      <Tabs defaultValue="components">
        <Tabs.List><Tabs.Tab value="components">Components</Tabs.Tab><Tabs.Tab value="structures">Structures</Tabs.Tab></Tabs.List>
        <Tabs.Panel value="components" pt="md">
          <Group mb="sm"><Button onClick={() => setEdit({ isNew: true, code: '', nameEn: '', nameNe: '', type: 'earning', formula: '', condition: '', dependsOnPaymentDays: false, taxable: true, rounding: 'paisa', sortOrder: 100, active: true })}>{t('create')}</Button></Group>
          <DataTable<SalaryComponent> rows={comps.data?.components ?? []} rowKey={(c) => c.code} loading={comps.loading} error={comps.error} emptyTitle="No components." emptyHint="Run the defaults seed from Setup." onRowClick={(c) => setEdit({ code: c.code, statutory: c.statutory, nameEn: c.name_en, nameNe: c.name_ne ?? '', type: c.type, formula: c.formula ?? '', condition: c.condition ?? '', dependsOnPaymentDays: c.depends_on_payment_days, taxable: c.taxable, rounding: c.rounding, sortOrder: c.sort_order, active: c.active })}
            columns={[
              { key: 'code', header: t('code'), render: (c) => <Group gap={4}><Text size="sm" fw={500}>{c.code}</Text>{c.statutory ? <Badge size="xs" variant="light" color="gray">🔒</Badge> : null}{!c.active ? <Badge size="xs" variant="light" color="red">inactive</Badge> : null}</Group> },
              { key: 'name', header: t('name'), render: (c) => `${c.name_en}${c.name_ne ? ` / ${c.name_ne}` : ''}` }, { key: 'type', header: 'Type' },
              { key: 'formula', header: 'Formula', render: (c) => <Text size="xs" ff="monospace">{c.formula ?? '(amount from assignment or input)'}</Text> },
              { key: 'flags', header: '', render: (c) => <Group gap={4}>{c.taxable ? <Badge size="xs" variant="outline">taxable</Badge> : null}{c.depends_on_payment_days ? <Badge size="xs" variant="outline">prorated</Badge> : null}</Group> },
            ]} />
        </Tabs.Panel>
        <Tabs.Panel value="structures" pt="md">
          <Group mb="sm"><Button onClick={() => setNewS({ code: '', nameEn: '', components: ['BASIC'] })}>{t('create')}</Button></Group>
          <DataTable<SalaryStructure> rows={structs.data?.structures ?? []} rowKey={(s) => s.id} loading={structs.loading} error={structs.error} emptyTitle="No structures."
            columns={[{ key: 'code', header: t('code') }, { key: 'name_en', header: t('name') },
              { key: 'versions', header: 'Versions', render: (s) => <Stack gap={2}>{(s.versions ?? []).map((v) => <Text key={v.id} size="xs">v{v.version}: {v.components.map((c) => c.code).join(', ')} {v.frozenAt ? '🔒' : ''}</Text>)}</Stack> },
              { key: 'a', header: '', render: (s) => <Button size="xs" variant="light" onClick={() => setNewS({ code: s.code, nameEn: s.name_en, components: (s.versions?.[s.versions.length - 1]?.components ?? []).map((c) => c.code) })}>New version</Button> }]} />
        </Tabs.Panel>
      </Tabs>
      <Modal opened={!!edit} onClose={() => setEdit(null)} title={edit?.isNew ? 'New component' : `${t('edit')} ${String(edit?.code)}`} size="lg" centered>
        {edit ? <Stack>
          {edit.statutory ? <Text size="xs" c="dimmed">🔒 {t('lockedStatutory')}: formula and type cannot change.</Text> : null}
          <SimpleGrid cols={{ base: 1, sm: 2 }}>
            <TextInput label={t('code')} value={String(edit.code)} onChange={(e) => set('code', e.currentTarget.value.toUpperCase().replace(/[^A-Z0-9_]/g, ''))} disabled={!edit.isNew} required />
            <Select label="Type" value={String(edit.type)} onChange={(v) => v && set('type', v)} allowDeselect={false} disabled={!!edit.statutory} data={['earning', 'deduction', 'employer_contribution', 'statistical']} />
            <TextInput label="Name" value={String(edit.nameEn)} onChange={(e) => set('nameEn', e.currentTarget.value)} required />
            <TextInput label="नाम" value={String(edit.nameNe ?? '')} onChange={(e) => set('nameNe', e.currentTarget.value)} />
          </SimpleGrid>
          <TextInput label="Formula" description={'e.g. BASIC * 0.11, or round(ot_hours * BASIC / hourly_divisor * rule("ot.multiplier", "working")). Blank = amount from assignment or one-time input.'} value={String(edit.formula ?? '')} onChange={(e) => set('formula', e.currentTarget.value)} disabled={!!edit.statutory} ff="monospace" />
          <TextInput label="Condition (optional)" description={'e.g. regime == "ssf"'} value={String(edit.condition ?? '')} onChange={(e) => set('condition', e.currentTarget.value)} ff="monospace" />
          <Group><Switch label="Prorate by paid days" checked={!!edit.dependsOnPaymentDays} onChange={(e) => set('dependsOnPaymentDays', e.currentTarget.checked)} /><Switch label="Taxable" checked={!!edit.taxable} onChange={(e) => set('taxable', e.currentTarget.checked)} /><Switch label="Active" checked={!!edit.active} onChange={(e) => set('active', e.currentTarget.checked)} /></Group>
          <Group><Select label="Rounding" value={String(edit.rounding)} onChange={(v) => v && set('rounding', v)} allowDeselect={false} data={['paisa', 'rupee']} w={120} /><TextInput label="Sort order" value={String(edit.sortOrder)} onChange={(e) => set('sortOrder', Number(e.currentTarget.value) || 0)} w={100} /></Group>
          <Group justify="flex-end"><Button loading={busy} onClick={save}>{t('save')}</Button></Group>
        </Stack> : null}
      </Modal>
      <Modal opened={!!newS} onClose={() => setNewS(null)} title="Salary structure" centered>
        {newS ? <Stack>
          <TextInput label={t('code')} value={newS.code} onChange={(e) => setNewS({ ...newS, code: e.currentTarget.value.toUpperCase() })} required />
          <TextInput label="Name" value={newS.nameEn} onChange={(e) => setNewS({ ...newS, nameEn: e.currentTarget.value })} required />
          <MultiSelect label="Components (in order)" value={newS.components} onChange={(v) => setNewS({ ...newS, components: v })} data={(comps.data?.components ?? []).map((c) => c.code)} searchable />
          <Group justify="flex-end"><Button loading={busy} disabled={!newS.code || !newS.nameEn || !newS.components.length} onClick={async () => { setBusy(true); try { const existing = structs.data?.structures.find((s) => s.code === newS.code); if (existing) await payApi.addVersion(existing.id, newS.components.map((code) => ({ code }))); else await payApi.createStructure({ code: newS.code, nameEn: newS.nameEn, components: newS.components.map((code) => ({ code })) }); toast.ok('Structure saved.'); setNewS(null); await structs.reload(); } catch (e) { toast.err(e); } finally { setBusy(false); } }}>{t('save')}</Button></Group>
        </Stack> : null}
      </Modal>
    </Stack>
  );
}
