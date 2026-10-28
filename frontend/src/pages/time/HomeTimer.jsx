import { Anchor } from '@mantine/core';
import { Link } from 'react-router-dom';
import { time as timeApi } from '../../api/time';
import { SectionTitle } from '../../components/PageHeader';
import { useQuery } from '../../lib/useQuery';
import { TimerCard } from './parts';

/** Compact project timer on Home; hidden when the company does not track time. */
export function HomeTimer() {
  const s = useQuery(() => timeApi.settings(), []);
  if (!s.data?.policy.enabled) return null;
  return (
    <div>
      <SectionTitle
        actions={
          <Anchor component={Link} to="/me/timesheet" size="sm">
            Open timesheet
          </Anchor>
        }
      >
        Project timer
      </SectionTitle>
      <TimerCard compact />
    </div>
  );
}
