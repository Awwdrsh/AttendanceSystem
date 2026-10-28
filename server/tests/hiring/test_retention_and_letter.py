"""Each company keeps rejected candidates for its own retention period; the offer letter escapes what HR typed."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from hajiri.db.models import HiringCandidate
from hajiri.hiring import services as hiring

from .world import future


def _rejected(hr, title: str, email: str) -> str:
    job = hiring.create_job(hr, {'title': title, 'description': 'A job description long enough to publish.'})
    app = hiring.add_candidate(hr, job, {'name': 'Kept Or Not', 'email': email, 'source': 'referral'})
    hiring.reject_application(hr, app, {'reason': 'Not this time', 'notifyCandidate': False})
    return app


def test_purge_uses_each_company_retention_period(world):
    _, hr_a = world.session(world.admin_a)
    _, hr_b = world.session(world.admin_b)
    hiring.set_policy(hr_a, {'retentionDays': 30, 'careersEnabled': True})
    hiring.set_policy(hr_b, {'retentionDays': 365, 'careersEnabled': True})
    _rejected(hr_a, 'Alpha role', 'a@x.test')
    _rejected(hr_b, 'Beta role', 'b@x.test')
    assert hiring.purge_rejected_candidates(datetime.now(timezone.utc) + timedelta(days=60)) == 1
    alpha = HiringCandidate.objects.for_company(world.alpha.id).get()
    beta = HiringCandidate.objects.for_company(world.beta.id).get()
    assert alpha.purged_at is not None and alpha.name == 'Removed candidate' and alpha.email is None
    assert beta.purged_at is None and beta.email == 'b@x.test'
    assert hiring.purge_rejected_candidates(datetime.now(timezone.utc) + timedelta(days=400)) == 1
    beta.refresh_from_db()
    assert beta.purged_at is not None


def test_offer_letter_escapes_typed_text_and_keeps_line_breaks(world):
    _, hr = world.session(world.admin_b)
    job = hiring.create_job(hr, {'title': 'Baker', 'description': 'Bake the bread our shops sell every morning.'})
    app = hiring.add_candidate(hr, job, {'name': 'Hari <b>Baker</b>', 'email': 'h@x.test', 'source': 'referral'})
    offer = hiring.create_offer(hr, app, {'designation': 'Baker & Co "lead"', 'salary': '30000', 'joinDate': future(20),
                                          'notes': 'Line one\nline two\n\n<script>x</script>'})
    letter = hiring.offer_letter(hr, offer)
    assert '<b>Baker</b>' not in letter and 'Hari &lt;b&gt;Baker&lt;/b&gt;' in letter
    assert 'Baker &amp; Co &quot;lead&quot;' in letter
    assert '<p>Line one<br>line two</p>' in letter and '<script>' not in letter
    assert 'class="draft"' in letter and 'NPR 30,000.00' in letter
