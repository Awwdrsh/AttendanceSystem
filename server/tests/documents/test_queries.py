"""The list pages of Documents & letters run a fixed number of queries, whatever the number of rows (no N+1)."""
from __future__ import annotations

import pytest
from django.db import connection
from django.test import Client
from django.test.utils import CaptureQueriesContext

from hajiri.db.models import OrgUnit
from hajiri.documents import services as docs
from tests.web.factories import htmx, sign_in

from .world import build, upload

pytestmark = pytest.mark.django_db


def client_for(user) -> Client:
    c = Client()
    sign_in(c, user)
    return c


def _count(c: Client, url: str, **extra) -> int:
    with CaptureQueriesContext(connection) as ctx:
        r = c.get(url, **extra)
    assert r.status_code == 200, (url, r.status_code)
    return len(ctx.captured_queries)


def _add_rows(w, n: int) -> None:
    """n policies of each audience kind, n letters, n letter requests and n documents to verify."""
    unit, _ = OrgUnit.objects.get_or_create(company_id=w.co.id, code='ENG',
                                            defaults={'name_en': 'Engineering', 'kind': OrgUnit.Kind.DEPARTMENT})
    for _ in range(n):
        docs.publish_policy(w.hr, {'title': 'Everyone', 'fileId': upload(w.hr, 'company_document')})
        docs.publish_policy(w.hr, {'title': 'Branch', 'fileId': upload(w.hr, 'company_document'), 'audience': 'branch',
                                   'audienceIds': [w.branch]})
        docs.publish_policy(w.hr, {'title': 'Dept', 'fileId': upload(w.hr, 'company_document'),
                                   'audience': 'department', 'audienceIds': [str(unit.id)]})
        docs.issue_letter(w.hr, {'templateId': w.template('confirmation'), 'employeeId': w.sita_id,
                                 'allowMissing': True})
        docs.add_document(w.sita, w.sita_id, {'categoryId': w.category(w.sita, 'academic'), 'title': 'Certificate',
                                              'fileId': upload(w.sita)})


PAGES = [
    ('hr', '/employees/documents?section=review', {}),
    ('hr', '/employees/documents?section=letters', {}),
    ('hr', '/employees/documents?section=policies', {}),
    ('hr', '/_/documents/policies', {'htmx': True}),
    ('hr', '/_/documents/review', {'htmx': True}),
    ('sita', '/me/profile/documents', {}),
    ('sita', '/_/documents/policies/mine', {'htmx': True}),
    ('sita', '/_/documents/docs', {'htmx': True}),
    ('sita', '/_/documents/letters', {'htmx': True}),
]


@pytest.mark.parametrize('who,url,opts', PAGES, ids=[p[1] for p in PAGES])
def test_list_pages_do_not_grow_with_rows(who, url, opts):
    w = build()
    _add_rows(w, 1)
    c = client_for(getattr(w, who))
    extra = htmx() if opts.get('htmx') else {}
    _count(c, url, **extra)          # warm caches (feature switches, seeded defaults)
    few = _count(c, url, **extra)
    _add_rows(w, 4)
    many = _count(c, url, **extra)
    assert many == few, f'{url}: {few} queries with few rows, {many} with more'


def test_audience_index_matches_audience_queryset():
    """The one-read audience of the policy lists is the same set, in the same order, as the per-policy query."""
    from hajiri.db.models import Branch, Employee
    from tests.web.factories import make_company, make_employee

    w = build()
    other_branch = str(Branch.objects.create(company_id=w.co.id, code='PKR', name_en='Pokhara').id)
    top = OrgUnit.objects.create(company_id=w.co.id, code='OPS', name_en='Operations', kind=OrgUnit.Kind.DEPARTMENT)
    mid = OrgUnit.objects.create(company_id=w.co.id, code='OPS1', name_en='Field', kind=OrgUnit.Kind.TEAM, parent=top)
    leaf = OrgUnit.objects.create(company_id=w.co.id, code='OPS2', name_en='Night', kind=OrgUnit.Kind.TEAM, parent=mid)
    side = OrgUnit.objects.create(company_id=w.co.id, code='HRD', name_en='HR', kind=OrgUnit.Kind.DEPARTMENT)
    for unit, branch in ((top, w.branch), (mid, other_branch), (leaf, other_branch), (side, w.branch), (None, None)):
        make_employee(w.co, org_unit=unit, branch=branch)
    gone = make_employee(w.co, org_unit=leaf, branch=other_branch)
    Employee.objects.filter(pk=gone.pk).update(status=Employee.Status.EXITED)
    # another company's employee in a unit id that is not this company's never counts
    make_employee(make_company(), branch=None)

    cid = str(w.co.id)
    cases = [
        {'audience': 'all', 'audience_ids': []},
        {'audience': 'branch', 'audience_ids': [w.branch]},
        {'audience': 'branch', 'audience_ids': [w.branch, other_branch]},
        {'audience': 'branch', 'audience_ids': []},
        {'audience': 'department', 'audience_ids': [str(top.id)]},
        {'audience': 'department', 'audience_ids': [str(mid.id)]},
        {'audience': 'department', 'audience_ids': [str(leaf.id), str(side.id)]},
        {'audience': 'department', 'audience_ids': []},
    ]
    index = docs.AudienceIndex(cid)
    for p in cases:
        assert index.ids(p) == docs.audience_ids(cid, p), p
        for one_id in (w.sita_id, str(gone.id)):
            assert docs.AudienceIndex(cid, one_id).ids(p) == docs.audience_ids(cid, p, one_id), (p, one_id)
