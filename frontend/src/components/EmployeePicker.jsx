import { Select } from '@mantine/core';
import { IconSearch } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { employees as empApi } from '../api';
/** Searchable employee selector for HR and manager forms. Options read "Name (code)". */
export function EmployeePicker({ value, onChange, label = 'Employee', required, status = 'active', ...rest }) {
  const [list, setList] = useState([]);
  useEffect(() => {
    empApi
      .list(status === 'all' ? {} : { status })
      .then((r) => setList(r.employees))
      .catch(() => setList([]));
  }, [status]);
  return (
    <Select
      label={label}
      {...(required !== undefined ? { required } : {})}
      searchable
      clearable
      value={value}
      placeholder="Search by name or code"
      leftSection={<IconSearch size={16} stroke={1.6} />}
      data={list.map((e) => ({ value: e.id, label: `${e.name_en} (${e.emp_code})` }))}
      onChange={(v) =>
        onChange(
          v,
          list.find((e) => e.id === v),
        )
      }
      nothingFoundMessage="No employee matches that search"
      {...rest}
    />
  );
}
