import { Anchor, Button } from '@mantine/core';
import { useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { useCompanies } from '../../auth/CompaniesProvider';
import { safeNext } from '../../lib/group';
import { toast } from '../../lib/toast';

/**
 * A link to a page of one company of the group. Group views never open another company's record in the active
 * company: when the company is not the active one, the link switches to it first and then opens `to` there.
 */
export function SwitchLink({ companyId, to, children, button, ...rest }) {
  const { active, switchTo } = useCompanies();
  const nav = useNavigate();
  const [busy, setBusy] = useState(false);
  const target = safeNext(to);
  const go = async (e) => {
    e?.preventDefault?.();
    if (active?.id === companyId) {
      nav(target);
      return;
    }
    setBusy(true);
    try {
      await switchTo(companyId, target);
    } catch (err) {
      toast.err(err, 'Could not switch company');
      setBusy(false);
    }
  };
  if (button)
    return (
      <Button size="xs" variant="light" loading={busy} onClick={go} {...rest}>
        {children}
      </Button>
    );
  return (
    <Anchor component="button" type="button" size="sm" onClick={go} disabled={busy} {...rest}>
      {children}
    </Anchor>
  );
}
