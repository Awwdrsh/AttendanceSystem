"""The Django-stack scripts (scripts/*-django.sh, scripts/django_ops.py) and the Docker files."""
import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / 'scripts'
SHELL_SCRIPTS = ['lib-django.sh', 'setup-django.sh', 'start-django.sh', 'stop-django.sh', 'run-django.sh', 'install-service-django.sh',
                 'update-django.sh']


def ops_module():
    spec = importlib.util.spec_from_file_location('django_ops', SCRIPTS / 'django_ops.py')
    mod = importlib.util.module_from_spec(spec)
    keep, sys.dont_write_bytecode = sys.dont_write_bytecode, True   # no __pycache__ in scripts/
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.dont_write_bytecode = keep
    return mod


def run_ops(*args):
    return subprocess.run([sys.executable, str(SCRIPTS / 'django_ops.py'), *args], capture_output=True, text=True, timeout=60)


@pytest.mark.parametrize('name', SHELL_SCRIPTS)
def test_shell_scripts_parse(name):
    path = SCRIPTS / name
    assert path.is_file()
    r = subprocess.run(['bash', '-n', str(path)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    if name != 'lib-django.sh':
        assert os.access(path, os.X_OK), f'{name} is not executable'


def test_docker_entrypoint_parses():
    r = subprocess.run(['sh', '-n', str(REPO / 'infra' / 'docker-django' / 'entrypoint.sh')], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_the_ts_scripts_are_still_there():
    for name in ('setup.sh', 'start.sh', 'stop.sh', 'run.sh', 'install-service.sh', 'update.sh', 'lib.sh'):
        assert (SCRIPTS / name).is_file()


def test_run_django_rejects_an_unknown_service():
    r = subprocess.run(['bash', str(SCRIPTS / 'run-django.sh'), 'api'], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert 'usage: run-django.sh web|worker|gateway' in r.stderr


def test_env_helpers_work_in_a_folder_with_spaces(tmp_path):
    folder = tmp_path / 'Attendance System '
    folder.mkdir()
    f = folder / '.env'
    f.write_text('PORT=3000\nEMPTY=\n')
    script = f'''source "{SCRIPTS / 'lib-django.sh'}"
env_set "{f}" EMPTY filled
env_set "{f}" NEW 'a=b/c+d'
env_set "{f}" PORT 3100
echo "$(env_get "{f}" PORT)|$(env_get "{f}" NEW)|$(env_get "{f}" MISSING fallback)|$(env_get "{f}" EMPTY)"
env_has_value "{f}" NEW && echo has
'''
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ['3100|a=b/c+d|fallback|filled', 'has']
    assert f.read_text() == 'PORT=3100\nEMPTY=filled\nNEW=a=b/c+d\n'


def test_service_ports_come_from_the_env_files(tmp_path):
    script = f'''source "{SCRIPTS / 'lib-django.sh'}"
WEB_ENV="{tmp_path}/web.env"; GW_ENV="{tmp_path}/gw.env"; ROOT="{tmp_path}"
echo "$(web_port) $(gateway_port) $(web_threads)"
printf 'PORT=3100\\nWEB_THREADS=4\\n' > "$WEB_ENV"; printf 'PORT=8180\\n' > "$GW_ENV"
echo "$(web_port) $(gateway_port) $(web_threads)"
'''
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ['3000 8080 8', '3100 8180 4']


def test_python_313_is_found():
    script = f'source "{SCRIPTS / "lib-django.sh"}"; p="$(find_python313)" && "$p" -c "import sys; print(sys.version_info[:2])"'
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=30)
    if r.returncode:
        pytest.skip('no Python 3.13 on PATH on this machine')
    assert r.stdout.strip() == '(3, 13)'


def test_ops_secrets():
    r = run_ops('secret', '32')
    assert r.returncode == 0 and len(base64.b64decode(r.stdout.strip())) == 32
    assert len(base64.b64decode(run_ops('secret', '24').stdout.strip())) == 24
    key = run_ops('signing-key').stdout.strip()
    from hajiri.gateway.punch import Signer
    signer = Signer.from_base64_pkcs8(key)
    assert not signer.ephemeral and 'BEGIN PUBLIC KEY' in signer.public_key_pem
    assert len(run_ops('password').stdout.strip()) == 16
    assert len(run_ops('django-secret').stdout.strip()) >= 50


def test_ops_usage():
    r = run_ops('nope')
    assert r.returncode == 1 and 'unknown command nope' in r.stderr
    assert run_ops().returncode == 1
    assert run_ops('--help').returncode == 0


def test_ops_health_fails_without_a_server(monkeypatch):
    mod = ops_module()
    monkeypatch.setenv('HAJIRI_SERVICE', 'web')
    monkeypatch.setenv('PORT', '1')
    assert mod.health() == 1


def test_ops_ensure_company_creates_the_company_and_its_head_office(db):
    from hajiri.core.db import rows
    mod = ops_module()
    cid = mod.ensure_company('ACME', "Acme's Pvt. Ltd.")
    assert mod.ensure_company('ACME', 'Other name') == cid
    assert rows('SELECT code, name_en FROM company WHERE id = %s', [cid]) == [{'code': 'ACME', 'name_en': "Acme's Pvt. Ltd."}]
    assert rows('SELECT code, name_en FROM branch WHERE company_id = %s', [cid]) == [{'code': 'HQ', 'name_en': 'Head Office'}]
    with pytest.raises(SystemExit, match='1-20 letters'):
        mod.ensure_company('bad code', 'x')


def test_compose_runs_web_worker_and_gateway_from_one_image():
    text = (REPO / 'infra' / 'docker-compose.django.yml').read_text()
    for svc in ('postgres:', 'web:', 'worker:', 'gateway:'):
        assert f'\n  {svc}' in text
    assert text.count('dockerfile: infra/docker-django/Dockerfile') == 1
    for cmd in ('["web"]', '["worker"]', '["gateway"]'):
        assert f'command: {cmd}' in text
    # The worker and the gateway wait for the web container, which applies the migrations.
    assert text.count('web: { condition: service_healthy }') == 2
    docker = (REPO / 'infra' / 'docker-django' / 'Dockerfile').read_text()
    assert docker.startswith('# The Django stack') and 'FROM python:3.13-' in docker
    ignore = (REPO / 'infra' / 'docker-django' / 'Dockerfile.dockerignore').read_text().split()
    for pattern in ('**/.env', '**/gateway.env', '**/.venv', '.git'):
        assert pattern in ignore


def test_docker_entrypoint_drops_blank_optional_settings(tmp_path):
    # Compose passes DJANGO_SECRET_KEY= (left blank in env.example) as an empty string; Django refuses an empty
    # SECRET_KEY, so the entrypoint unsets it and the settings fall back to PASSWORD_PEPPER.
    env = {'PATH': os.environ['PATH'], 'HAJIRI_SERVER_DIR': str(tmp_path), 'DJANGO_SECRET_KEY': '', 'ALLOWED_HOSTS': '', 'PASSWORD_PEPPER': 'p'}
    r = subprocess.run(['sh', str(REPO / 'infra' / 'docker-django' / 'entrypoint.sh'), 'env'], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert not any(line.startswith(('DJANGO_SECRET_KEY=', 'ALLOWED_HOSTS=')) for line in lines)
    assert 'HAJIRI_SERVICE=env' in lines and 'PASSWORD_PEPPER=p' in lines
    env.update(DJANGO_SECRET_KEY='kept', ALLOWED_HOSTS='hr.example.com')
    lines = subprocess.run(['sh', str(REPO / 'infra' / 'docker-django' / 'entrypoint.sh'), 'env'], capture_output=True, text=True, env=env,
                           timeout=30).stdout.splitlines()
    assert 'DJANGO_SECRET_KEY=kept' in lines and 'ALLOWED_HOSTS=hr.example.com' in lines


def test_ops_starts_django_with_a_blank_secret_key():
    # The container health check runs django_ops.py directly (not through the entrypoint), with the same blank value.
    env = {**os.environ, 'DJANGO_SECRET_KEY': '', 'ALLOWED_HOSTS': ''}
    r = subprocess.run([sys.executable, str(SCRIPTS / 'django_ops.py'), 'db-name'], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()


def test_setup_tells_how_to_reset_the_admin_password():
    text = (SCRIPTS / 'setup-django.sh').read_text()
    assert 'reset_password --email $ADMIN_EMAIL --password <new>' in text
