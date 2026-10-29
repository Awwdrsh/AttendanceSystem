"""Browser tests against a running Hajiri (see test_browser.py). Skipped unless HAJIRI_E2E_URL is set."""
import json
import os

import pytest

BASE = os.environ.get('HAJIRI_E2E_URL', '').rstrip('/')


def pytest_collection_modifyitems(config, items):
    if BASE:
        return
    skip = pytest.mark.skip(reason='browser tests: set HAJIRI_E2E_URL (and HAJIRI_E2E_USERS) to run them')
    for item in items:
        if '/e2e/' in str(item.fspath).replace(os.sep, '/'):
            item.add_marker(skip)


@pytest.fixture(scope='session')
def base_url():
    return BASE


@pytest.fixture(scope='session')
def users():
    path = os.environ.get('HAJIRI_E2E_USERS')
    if not path:
        pytest.skip('HAJIRI_E2E_USERS: the JSON written by tests/e2e/seed_users.py')
    text = open(path, encoding='utf-8').read()
    return json.loads(text.split('E2EJSON', 1)[-1])


@pytest.fixture(scope='session')
def browser():
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(headless=os.environ.get('HAJIRI_E2E_HEADED') != '1')
        yield b
        b.close()
