import { Select, type SelectProps } from '@mantine/core';
import { IconSearch } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { employees as empApi } from '../api';
import type { Employee } from '../api/types';

/** Searchable employee selector for HR and manager forms. Options read "Name (code)". */
export function EmployeePicker({ value, onChange, label = 'Employee', required, status = 'active', ...rest }: { value: string | null; onChange: (id: string | null, emp?: Employee) => void; label?: string; required?: boolean; status?: 'active' | 'exited' | 'all' } & Omit<SelectProps, 'value' | 'onChange' | 'data' | 'label'>) {
  const [list, setList] = useState<Employee[]>([]);
  useEffect(() => { empApi.list(status === 'all' ? {} : { status }).then((r) => setList(r.employees)).catch(() => setList([])); }, [status]);
  return (
    <Select label={label} {...(required !== undefined ? { required } : {})} searchable clearable value={value} placeholder="Search by name or code"
      leftSection={<IconSearch size={16} stroke={1.6} />}
      data={list.map((e) => ({ value: e.id, label: `${e.name_en} (${e.emp_code})` }))}
      onChange={(v) => onChange(v, list.find((e) => e.id === v))} nothingFoundMessage="No employee matches that search" {...rest} />
  );
}
