from tests.web.factories import signed_in_client


def test_the_home_and_settings_pages_data_loads(acme):
    """What Home, Notifications, Settings, Users, Features and Setup read, for an HR admin."""
    c, _ = signed_in_client(acme, roles=['hr', 'admin'])
    for url in ['/api/v1/auth/me', '/api/v1/dashboard/me', '/api/v1/notifications', '/api/v1/settings',
                '/api/v1/settings/company', '/api/v1/settings/branches', '/api/v1/settings/org-units',
                '/api/v1/auth/users', '/api/v1/settings/features', '/api/v1/me/companies']:
        r = c.get(url)
        assert r.status_code == 200, (url, r.status_code, r.content[:2000])
