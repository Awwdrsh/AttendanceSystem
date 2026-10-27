from tests.web.factories import signed_in_client


def test_pages_render(acme):
    c, _ = signed_in_client(acme, roles=['hr', 'admin'])
    for url in ['/', '/me/notifications', '/settings', '/settings/users', '/settings/features', '/settings/setup',
                *[f'/settings?tab={t}' for t in ('leave', 'payroll', 'time', 'notify', 'company', 'org', 'security')]]:
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code, r.content[:2000])
