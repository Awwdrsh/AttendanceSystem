import { Button, Grid, Group, Loader, SegmentedControl, Stack } from '@mantine/core';
import { IconConfetti, IconHeartHandshake, IconSettings, IconSpeakerphone } from '@tabler/icons-react';
import { useEffect, useState } from 'react';
import { engage, type FeedItem } from '../../api/engage';
import { useAuth } from '../../auth/AuthProvider';
import { AnnouncementComposer, KudosComposer, ValuesManager } from '../../components/engage/Composers';
import { AnnouncementCard, KudosCard } from '../../components/engage/FeedCards';
import { CelebrationsPanel, LeaderboardPanel, SuggestionBoxPanel } from '../../components/engage/Panels';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader } from '../../components/PageHeader';
import { useFeatures } from '../../lib/features';
import { toast } from '../../lib/toast';

type Filter = 'all' | 'kudos' | 'announcement';

/** The company feed: announcements and kudos, newest first, with celebrations, the leaderboard and the suggestion box alongside. */
export default function EngageFeedPage() {
  const { user, hasRole } = useAuth();
  const kudosOn = useFeatures().on('kudos');
  const suggestionsOn = useFeatures().on('suggestions');
  const [items, setItems] = useState<FeedItem[]>([]);
  const [next, setNext] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const [filter, setFilter] = useState<Filter>('all');
  const [kudosOpen, setKudosOpen] = useState(false);
  const [annOpen, setAnnOpen] = useState(false);
  const [valuesOpen, setValuesOpen] = useState(false);
  const [version, setVersion] = useState(0);

  const load = async (before?: string) => {
    setLoading(true);
    try {
      const r = await engage.feed(before ? { before, limit: 20 } : { limit: 20 });
      setItems((cur) => (before ? [...cur, ...r.items] : r.items));
      setNext(r.nextBefore);
    } catch (e) { toast.err(e, 'Could not load the feed'); } finally { setLoading(false); }
  };
  useEffect(() => { void load(); }, [version]);
  const refresh = () => setVersion((v) => v + 1);
  const shown = items.filter((i) => filter === 'all' || i.type === filter);
  const pinned = shown.filter((i) => i.type === 'announcement' && i.pinned);
  const rest = shown.filter((i) => !(i.type === 'announcement' && i.pinned));

  return (
    <>
      <PageHeader subtitle="Say thanks, catch up on company news and celebrate each other."
        actions={<>
          {hasRole('hr') && kudosOn ? <Button variant="subtle" color="gray" leftSection={<IconSettings size={16} stroke={1.6} />} onClick={() => setValuesOpen(true)}>Kudos settings</Button> : null}
          {hasRole('hr', 'manager') ? <Button variant="default" leftSection={<IconSpeakerphone size={16} stroke={1.6} />} onClick={() => setAnnOpen(true)}>Announce</Button> : null}
          {user?.employeeId && kudosOn ? <Button leftSection={<IconHeartHandshake size={16} stroke={1.6} />} onClick={() => setKudosOpen(true)}>Give kudos</Button> : null}
        </>} title="Engage" />
      <Grid gap="lg">
        <Grid.Col span={{ base: 12, md: 8 }}>
          <Stack gap="md">
            {kudosOn ? <Group justify="space-between">
              <SegmentedControl size="xs" value={filter} onChange={(v) => setFilter(v as Filter)}
                data={[{ value: 'all', label: 'Everything' }, { value: 'kudos', label: 'Kudos' }, { value: 'announcement', label: 'Announcements' }]} />
            </Group> : null}
            {[...pinned, ...rest].map((i) => (i.type === 'kudos'
              ? <KudosCard key={`k${i.id}`} k={i} onRemoved={refresh} />
              : <AnnouncementCard key={`a${i.id}`} a={i} onChanged={refresh} />))}
            {loading ? <Group justify="center" py="lg"><Loader size="sm" /></Group> : null}
            {!loading && shown.length === 0 ? (
              <EmptyState icon={<IconConfetti size={20} stroke={1.6} />} title={filter === 'announcement' ? 'No announcements yet' : 'Nothing here yet'}
                hint="Thank a colleague for something they did this week. It takes a minute and makes their day."
                {...(user?.employeeId && kudosOn ? { action: { label: 'Give kudos', onClick: () => setKudosOpen(true) } } : {})} />
            ) : null}
            {next && !loading ? <Button variant="default" onClick={() => void load(next)}>Show older</Button> : null}
          </Stack>
        </Grid.Col>
        <Grid.Col span={{ base: 12, md: 4 }}>
          <Stack gap="md">
            <CelebrationsPanel />
            {kudosOn ? <LeaderboardPanel key={version} /> : null}
            {suggestionsOn ? <SuggestionBoxPanel /> : null}
          </Stack>
        </Grid.Col>
      </Grid>
      <KudosComposer opened={kudosOpen} onClose={() => setKudosOpen(false)} onDone={refresh} />
      <AnnouncementComposer opened={annOpen} onClose={() => setAnnOpen(false)} onDone={refresh} />
      {hasRole('hr') ? <ValuesManager opened={valuesOpen} onClose={() => setValuesOpen(false)} /> : null}
    </>
  );
}
