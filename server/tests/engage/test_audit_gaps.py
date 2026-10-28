"""Gaps found when auditing the engage port against the TS code and React pages."""
from tests.support.sql import execute, one
from hajiri.core.errors import BadRequestError
from hajiri.engage import services as S
from tests.web.factories import htmx, toasts

import pytest

from .conftest import client_for


def test_show_older_redraws_every_loaded_page_with_pinned_first(world):
    """EngageFeedPage applies the filter and the pinned-first order to everything loaded so far, not page by page."""
    w = world
    old = S.post_announcement(w.hr, {'title': 'Old pinned notice', 'body': 'Still relevant', 'pinned': True})['id']
    execute("UPDATE announcement SET created_at = now() - interval '2 days' WHERE id = %s", [old])
    for i in range(20):
        k = S.give_kudos(w.sita, {'recipientIds': [w.ram_id], 'message': f'Thanks number {i}'})['id']
        execute("UPDATE kudos SET created_at = now() - make_interval(mins => %s) WHERE id = %s", [i + 1, k])
    c = client_for(w.ram)
    first = c.get('/_/engage/feed', **htmx()).content.decode()
    assert 'Old pinned notice' not in first and 'Show older' in first and 'pages=2' in first
    both = c.get('/_/engage/feed?pages=2', **htmx()).content.decode()
    assert both.index('Old pinned notice') < both.index('Thanks number 0')
    assert both.count('gave kudos to') == 20 and 'Show older' not in both
    only = c.get('/_/engage/feed?pages=2&show=announcement', **htmx()).content.decode()
    assert 'Old pinned notice' in only and 'gave kudos to' not in only and 'No announcements yet' not in only
    # A filter that matches nothing on any loaded page shows the empty state, even after loading older items.
    execute('UPDATE announcement SET deleted_at = now()')
    assert 'No announcements yet' in c.get('/_/engage/feed?pages=2&show=announcement', **htmx()).content.decode()


def test_malformed_target_ids_are_refused_like_the_database_did(world):
    """TS sent a 36-character id the regex let through to PostgreSQL, which refused anything that is not a uuid."""
    w = world
    bad = '-' * 36
    for target_type, target_id in (('kudos', bad), ('announcement', bad), ('celebration', f'joined:{bad}')):
        with pytest.raises(BadRequestError) as e:
            S.react(w.ram, {'targetType': target_type, 'targetId': target_id, 'emoji': 'like'})
        assert e.value.message == 'invalid value format'
    r = client_for(w.ram).post('/_/engage/comments', {'target_type': 'celebration', 'target_id': f'joined:{bad}', 'body': 'Hi'}, **htmx())
    assert r.status_code == 400 and toasts(r)[0]['message'] == 'invalid value format'
    # Hyphens after any group of four hex digits are a valid uuid to PostgreSQL: it is looked up and not found.
    loose = 'a0ee-bc99-9c0b-4ef8-bb6d6bb9bd380a11'
    assert not S.celebration_target_ok(w.company_id, f'joined:{loose}')
    # A well-formed id that zod would not call a uuid (version 0) is still looked up, as TS did.
    assert not S.can_see_target(w.ram, 'kudos', '00000000-0000-0000-0000-000000000000')


def test_reply_form_starts_with_the_stored_sharing_choice(world):
    """SuggestionsPage: setShare(s.isPublic ?? true); a new suggestion is not shared, so the switch starts off."""
    w = world
    S.suggest(w.sita, {'body': 'A quiet room for calls'})
    sid = str(one('SELECT id FROM suggestion')['id'])
    hr = client_for(w.hr)
    form = hr.get(f'/_/engage/suggestions/{sid}/reply', **htmx()).content.decode()
    assert 'name="is_public" value="1">' in form and 'value="1" checked' not in form
    S.update_suggestion(w.hr, sid, {'reply': 'Booked room 3.', 'isPublic': True})
    form = hr.get(f'/_/engage/suggestions/{sid}/reply', **htmx()).content.decode()
    assert 'name="is_public" value="1" checked' in form
    assert client_for(w.foreign_hr).get(f'/_/engage/suggestions/{sid}/reply', **htmx()).status_code == 404
    assert client_for(w.sita).get(f'/_/engage/suggestions/{sid}/reply', **htmx()).status_code == 403


def test_kudos_form_says_when_nobody_matches(world):
    form = client_for(world.sita).get('/_/engage/kudos/new', **htmx()).content.decode()
    assert 'Nobody matches that name' in form and '&quot;ram thapa&quot;' in form


def test_poll_results_replace_the_answer_form_in_the_modal(world):
    """After voting the results open in the modal with their own title (SurveysPage opens the results modal)."""
    w = world
    t = S.npt_today()
    pid = S.create_survey(w.hr, {'kind': 'poll', 'title': 'Snacks', 'anonymous': False, 'opensOn': t, 'closesOn': t,
                                 'questions': [{'kind': 'single', 'prompt': 'Which?', 'options': ['Momo', 'Samosa']}]})['id']
    S.publish_survey(w.hr, pid)
    form = client_for(w.sita).get(f'/_/engage/surveys/{pid}/answer', **htmx()).content.decode()
    assert 'hx-target="#hj-modal-body" hx-swap="innerHTML"' in form
    q = S.get_survey(w.hr, pid)['questions'][0]
    r = client_for(w.sita).post(f'/_/engage/surveys/{pid}/answer', {f"q-{q['id']}": 'Momo'}, **htmx())
    assert 'data-modal-title="Results: Snacks"' in r.content.decode()
