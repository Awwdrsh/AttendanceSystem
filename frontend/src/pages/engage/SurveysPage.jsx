import { ActionIcon, Badge, Button, Group, Menu, Modal, Paper, SimpleGrid, Stack, Table, Text } from '@mantine/core';
import {
  IconChartBar,
  IconDots,
  IconDownload,
  IconEdit,
  IconLock,
  IconPlayerStop,
  IconPlus,
  IconSend,
  IconTrash,
  IconClipboardList,
} from '@tabler/icons-react';
import { useState } from 'react';
import { download } from '../../api/client';
import { engage } from '../../api/engage';
import { useAuth } from '../../auth/AuthProvider';
import { SurveyAnswerModal, SurveyBuilder, SurveyResultsView, statusBadge } from '../../components/engage/SurveyForms';
import { EmptyState } from '../../components/EmptyState';
import { PageHeader, SectionTitle } from '../../components/PageHeader';
import { fmtDate } from '../../lib/format';
import { toast } from '../../lib/toast';
import { useQuery } from '../../lib/useQuery';

const REPEAT = { none: '', weekly: 'Weekly', monthly: 'Monthly' };

/** Surveys and polls: answer the ones open to you; HR builds, publishes, closes and reads results. */
export default function SurveysPage() {
  const { user, hasRole } = useAuth();
  const isHr = hasRole('hr');
  const mine = useQuery(() => engage.surveys(), [], { enabled: !!user?.employeeId });
  const manage = useQuery(() => engage.manageSurveys(), [], { enabled: isHr });
  const [answering, setAnswering] = useState(null);
  const [builder, setBuilder] = useState({ open: false, editing: null });
  const [results, setResults] = useState(null);

  const showResults = async (id) => {
    try {
      setResults(await engage.results(id));
    } catch (e) {
      toast.err(e);
    }
  };
  const act = async (fn, ok) => {
    try {
      await fn();
      toast.ok(ok);
      await manage.reload();
      await mine.reload();
    } catch (e) {
      toast.err(e);
    }
  };
  const open = mine.data?.surveys ?? [];

  return (
    <>
      <PageHeader
        title="Surveys"
        subtitle="Share how things are going. Anonymous surveys never store who answered."
        actions={
          isHr ? (
            <Button leftSection={<IconPlus size={16} />} onClick={() => setBuilder({ open: true, editing: null })}>
              New survey or poll
            </Button>
          ) : undefined
        }
      />
      <Stack gap="xl">
        {user?.employeeId ? (
          <div>
            <SectionTitle hint="Surveys and polls you can answer now.">Open for you</SectionTitle>
            {!mine.loading && open.length === 0 ? (
              <EmptyState
                compact
                icon={<IconClipboardList size={20} stroke={1.6} />}
                title="Nothing to answer right now"
                hint="When HR opens a survey or poll for you it shows up here and on Home."
              />
            ) : (
              <SimpleGrid cols={{ base: 1, sm: 2, lg: 3 }}>
                {open.map((s) => (
                  <Paper key={s.id} withBorder p="md" radius="lg">
                    <Stack gap={6} h="100%" justify="space-between">
                      <div>
                        <Group gap={6} mb={4}>
                          <Badge size="xs" variant="light" color={s.kind === 'poll' ? 'blue' : 'brand'}>
                            {s.kind === 'poll' ? 'Poll' : 'Survey'}
                          </Badge>
                          {s.anonymous ? (
                            <Badge size="xs" variant="outline" color="gray" leftSection={<IconLock size={10} />}>
                              Anonymous
                            </Badge>
                          ) : null}
                        </Group>
                        <Text fw={600}>{s.title}</Text>
                        <Text size="xs" c="dimmed">
                          {s.questionCount} {s.questionCount === 1 ? 'question' : 'questions'} · until {fmtDate(s.closesOn)}
                        </Text>
                      </div>
                      {s.answered ? (
                        <Group justify="space-between" mt="sm">
                          <Text size="sm" c="dimmed">
                            Answered, thank you
                          </Text>
                          {s.kind === 'poll' ? (
                            <Button size="xs" variant="subtle" onClick={() => void showResults(s.id)}>
                              See results
                            </Button>
                          ) : null}
                        </Group>
                      ) : (
                        <Button mt="sm" size="sm" onClick={() => setAnswering(s.id)}>
                          {s.kind === 'poll' ? 'Vote' : 'Answer'}
                        </Button>
                      )}
                    </Stack>
                  </Paper>
                ))}
              </SimpleGrid>
            )}
          </div>
        ) : null}

        {isHr ? (
          <div>
            <SectionTitle hint="Drafts can be edited. Once published, questions are fixed; you can move the closing day or close early.">
              All surveys
            </SectionTitle>
            {!manage.loading && (manage.data?.surveys.length ?? 0) === 0 ? (
              <EmptyState
                compact
                title="No surveys yet"
                hint="Start with a monthly pulse: one eNPS question, two ratings and a comment box."
                action={{ label: 'New survey', onClick: () => setBuilder({ open: true, editing: null }) }}
              />
            ) : (
              <Paper withBorder radius="lg">
                <Table.ScrollContainer minWidth={640}>
                  <Table verticalSpacing="sm" highlightOnHover>
                    <Table.Thead>
                      <Table.Tr>
                        <Table.Th>Title</Table.Th>
                        <Table.Th>Status</Table.Th>
                        <Table.Th>Dates</Table.Th>
                        <Table.Th ta="right">Answers</Table.Th>
                        <Table.Th />
                      </Table.Tr>
                    </Table.Thead>
                    <Table.Tbody>
                      {manage.data?.surveys.map((s) => (
                        <Table.Tr key={s.id}>
                          <Table.Td>
                            <Text size="sm" fw={500}>
                              {s.title}
                            </Text>
                            <Text size="xs" c="dimmed">
                              {s.kind === 'poll' ? 'Poll' : 'Survey'}
                              {s.anonymous ? ' · anonymous' : ''}
                              {REPEAT[s.recurrence] ? ` · ${REPEAT[s.recurrence]}` : ''}
                            </Text>
                          </Table.Td>
                          <Table.Td>{statusBadge(s)}</Table.Td>
                          <Table.Td>
                            <Text size="sm">
                              {fmtDate(s.opensOn)} to {fmtDate(s.closesOn)}
                            </Text>
                          </Table.Td>
                          <Table.Td ta="right" className="num">
                            {s.responses ?? 0}
                          </Table.Td>
                          <Table.Td>
                            <Group gap={4} justify="flex-end" wrap="nowrap">
                              {s.status !== 'draft' ? (
                                <Button
                                  size="compact-sm"
                                  variant="subtle"
                                  leftSection={<IconChartBar size={14} />}
                                  onClick={() => void showResults(s.id)}
                                >
                                  Results
                                </Button>
                              ) : null}
                              <Menu position="bottom-end" withinPortal>
                                <Menu.Target>
                                  <ActionIcon variant="subtle" color="gray" aria-label={`Actions for ${s.title}`}>
                                    <IconDots size={16} />
                                  </ActionIcon>
                                </Menu.Target>
                                <Menu.Dropdown>
                                  {s.status === 'draft' ? (
                                    <>
                                      <Menu.Item
                                        leftSection={<IconSend size={14} />}
                                        onClick={() =>
                                          void act(
                                            () => engage.publishSurvey(s.id),
                                            'Published. People in the audience have been notified.',
                                          )
                                        }
                                      >
                                        Publish
                                      </Menu.Item>
                                      <Menu.Item
                                        leftSection={<IconEdit size={14} />}
                                        onClick={() => setBuilder({ open: true, editing: s })}
                                      >
                                        Edit
                                      </Menu.Item>
                                      <Menu.Item
                                        color="red"
                                        leftSection={<IconTrash size={14} />}
                                        onClick={() => void act(() => engage.deleteSurvey(s.id), 'Draft deleted.')}
                                      >
                                        Delete
                                      </Menu.Item>
                                    </>
                                  ) : null}
                                  {s.status === 'published' ? (
                                    <Menu.Item
                                      leftSection={<IconPlayerStop size={14} />}
                                      onClick={() => void act(() => engage.closeSurvey(s.id), 'Closed.')}
                                    >
                                      Close now{s.recurrence !== 'none' ? ' and stop repeating' : ''}
                                    </Menu.Item>
                                  ) : null}
                                  {s.status !== 'draft' ? (
                                    <Menu.Item
                                      leftSection={<IconDownload size={14} />}
                                      onClick={() => {
                                        download(`/engage/surveys/${s.id}/results.csv`, `survey-results.csv`).catch((e) => toast.err(e));
                                      }}
                                    >
                                      Download results (CSV)
                                    </Menu.Item>
                                  ) : null}
                                </Menu.Dropdown>
                              </Menu>
                            </Group>
                          </Table.Td>
                        </Table.Tr>
                      ))}
                    </Table.Tbody>
                  </Table>
                </Table.ScrollContainer>
              </Paper>
            )}
          </div>
        ) : null}
      </Stack>

      <SurveyAnswerModal
        id={answering}
        opened={!!answering}
        onClose={() => setAnswering(null)}
        onDone={(r) => {
          const s = open.find((x) => x.id === answering);
          void mine.reload();
          if (r && s) setResults({ survey: { id: s.id, title: s.title, anonymous: s.anonymous }, results: r });
        }}
      />
      {isHr ? (
        <SurveyBuilder
          opened={builder.open}
          editing={builder.editing}
          onClose={() => setBuilder({ open: false, editing: null })}
          onSaved={() => void manage.reload()}
        />
      ) : null}
      <Modal
        opened={!!results}
        onClose={() => setResults(null)}
        title={results ? `Results: ${results.survey.title}` : ''}
        centered
        size="xl"
      >
        {results ? <SurveyResultsView results={results.results} anonymous={results.survey.anonymous} /> : null}
      </Modal>
    </>
  );
}
