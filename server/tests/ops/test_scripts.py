"""The shell scripts (scripts/*.sh, scripts/django_ops.py) and the Docker files."""
import base64
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[3]
SCRIPTS = REPO / 'scripts'
SHELL_SCRIPTS = ['lib.sh', 'setup.sh', 'start.sh', 'stop.sh', 'run.sh', 'install-service.sh', 'update.sh',
                 'build-frontend.sh']


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
    if name != 'lib.sh':
        assert os.access(path, os.X_OK), f'{name} is not executable'


def test_docker_entrypoint_parses():
    r = subprocess.run(['sh', '-n', str(REPO / 'infra' / 'docker' / 'entrypoint.sh')], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_no_script_runs_node_or_reads_the_ts_env_files():
    """Installs need only Python: Node.js runs only in build-frontend.sh (lib.sh checks its version, update.sh calls
    the build script when Node.js is there)."""
    for path in SCRIPTS.glob('*.sh'):
        text = path.read_text()
        assert 'pnpm' not in text and '-django.sh' not in text, path.name
        if path.name not in ('build-frontend.sh', 'lib.sh'):
            assert 'node ' not in text and 'npm ' not in text and 'npx ' not in text, path.name
        # apps/api/.env and apps/gateway/.env are only read by setup.sh, once, to move an old install's secrets over.
        assert path.name == 'setup.sh' or 'apps/' not in text, path.name


def test_run_rejects_an_unknown_service():
    r = subprocess.run(['bash', str(SCRIPTS / 'run.sh'), 'api'], capture_output=True, text=True, timeout=30)
    assert r.returncode == 1
    assert 'usage: run.sh web|worker|gateway' in r.stderr


def test_env_helpers_work_in_a_folder_with_spaces(tmp_path):
    folder = tmp_path / 'Attendance System '
    folder.mkdir()
    f = folder / '.env'
    f.write_text('PORT=3000\nEMPTY=\n')
    script = f'''source "{SCRIPTS / 'lib.sh'}"
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
    script = f'''source "{SCRIPTS / 'lib.sh'}"
WEB_ENV="{tmp_path}/web.env"; GW_ENV="{tmp_path}/gw.env"; ROOT="{tmp_path}"
echo "$(web_port) $(gateway_port) $(web_threads)"
printf 'PORT=3100\\nWEB_THREADS=4\\n' > "$WEB_ENV"; printf 'PORT=8180\\n' > "$GW_ENV"
echo "$(web_port) $(gateway_port) $(web_threads)"
'''
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == ['3000 8080 8', '3100 8180 4']


def test_python_313_is_found():
    script = f'source "{SCRIPTS / "lib.sh"}"; p="$(find_python313)" && "$p" -c "import sys; print(sys.version_info[:2])"'
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
    from .sql import rows
    mod = ops_module()
    cid = mod.ensure_company('ACME', "Acme's Pvt. Ltd.")
    assert mod.ensure_company('ACME', 'Other name') == cid
    assert rows('SELECT code, name_en FROM company WHERE id = %s', [cid]) == [{'code': 'ACME', 'name_en': "Acme's Pvt. Ltd."}]
    assert rows('SELECT code, name_en FROM branch WHERE company_id = %s', [cid]) == [{'code': 'HQ', 'name_en': 'Head Office'}]
    with pytest.raises(SystemExit, match='1-20 letters'):
        mod.ensure_company('bad code', 'x')


def test_compose_runs_web_worker_and_gateway_from_one_image():
    text = (REPO / 'infra' / 'docker-compose.yml').read_text()
    for svc in ('postgres:', 'web:', 'worker:', 'gateway:'):
        assert f'\n  {svc}' in text
    assert text.count('dockerfile: infra/docker/Dockerfile') == 1
    for cmd in ('["web"]', '["worker"]', '["gateway"]'):
        assert f'command: {cmd}' in text
    # The worker, the gateway and the split profile's scheduler and task workers wait for the web container, which
    # applies the migrations.
    assert text.count('web: { condition: service_healthy }') == 4
    # The split deployment: one scheduler and any number of task workers, only with --profile split.
    for svc in ('scheduler', 'tasks'):
        block = text.split(f'\n  {svc}:\n', 1)[1].split('\n  gateway:', 1)[0].split('\n  tasks:', 1)[0]
        assert f'command: ["{svc}"]' in block and 'profiles: ["split"]' in block and f'HAJIRI_SERVICE: {svc}' in block
    assert '--profile split' in text and '--scale worker=0' in text
    docker = (REPO / 'infra' / 'docker' / 'Dockerfile').read_text()
    assert docker.startswith('# Hajiri in one image') and 'FROM python:3.13-' in docker
    ignore = (REPO / 'infra' / 'docker' / 'Dockerfile.dockerignore').read_text().split()
    for pattern in ('**/.env', '**/gateway.env', '**/.venv', '.git'):
        assert pattern in ignore


def test_docker_entrypoint_drops_blank_optional_settings(tmp_path):
    # Compose passes DJANGO_SECRET_KEY= (left blank in env.example) as an empty string; Django refuses an empty
    # SECRET_KEY, so the entrypoint unsets it and the settings fall back to PASSWORD_PEPPER.
    env = {'PATH': os.environ['PATH'], 'HAJIRI_SERVER_DIR': str(tmp_path), 'DJANGO_SECRET_KEY': '', 'ALLOWED_HOSTS': '', 'PASSWORD_PEPPER': 'p'}
    r = subprocess.run(['sh', str(REPO / 'infra' / 'docker' / 'entrypoint.sh'), 'env'], capture_output=True, text=True, env=env, timeout=30)
    assert r.returncode == 0, r.stderr
    lines = r.stdout.splitlines()
    assert not any(line.startswith(('DJANGO_SECRET_KEY=', 'ALLOWED_HOSTS=')) for line in lines)
    assert 'HAJIRI_SERVICE=env' in lines and 'PASSWORD_PEPPER=p' in lines
    env.update(DJANGO_SECRET_KEY='kept', ALLOWED_HOSTS='hr.example.com')
    lines = subprocess.run(['sh', str(REPO / 'infra' / 'docker' / 'entrypoint.sh'), 'env'], capture_output=True, text=True, env=env,
                           timeout=30).stdout.splitlines()
    assert 'DJANGO_SECRET_KEY=kept' in lines and 'ALLOWED_HOSTS=hr.example.com' in lines


def test_ops_starts_django_with_a_blank_secret_key():
    # The container health check runs django_ops.py directly (not through the entrypoint), with the same blank value.
    env = {**os.environ, 'DJANGO_SECRET_KEY': '', 'ALLOWED_HOSTS': ''}
    r = subprocess.run([sys.executable, str(SCRIPTS / 'django_ops.py'), 'db-name'], capture_output=True, text=True, env=env, timeout=60)
    assert r.returncode == 0, r.stderr
    assert r.stdout.strip()


def test_setup_tells_how_to_reset_the_admin_password():
    text = (SCRIPTS / 'setup.sh').read_text()
    assert 'reset_password --email $ADMIN_EMAIL --password <new>' in text


def test_worker_mode_picks_the_background_services(tmp_path):
    script = f'''source "{SCRIPTS / 'lib.sh'}"
WEB_ENV="{tmp_path}/web.env"
echo "$(django_services)"
printf 'WORKER_MODE=split\\n' > "$WEB_ENV"; echo "$(django_services)"
printf 'WORKER_MODE=split\\nTASK_WORKERS=3\\n' > "$WEB_ENV"; echo "$(django_services)"
printf 'WORKER_MODE=split\\nTASK_WORKERS=many\\n' > "$WEB_ENV"; echo "$(django_services)"
for s in worker scheduler tasks tasks-2 web gateway; do is_background_service "$s" && printf '%s ' "$s"; done; echo
'''
    r = subprocess.run(['bash', '-c', script], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert r.stdout.splitlines() == [
        'gateway worker web',
        'gateway scheduler tasks web',
        'gateway scheduler tasks tasks-2 tasks-3 web',
        'gateway scheduler tasks web',
        'worker scheduler tasks tasks-2 ',
    ]


def test_run_knows_the_split_services():
    text = (SCRIPTS / 'run.sh').read_text()
    assert 'web|worker|gateway|scheduler|tasks|tasks-[0-9]*)' in text
    lib = (SCRIPTS / 'lib.sh').read_text()
    assert 'manage.py run_scheduler' in lib and 'manage.py db_worker --queue-name default' in lib


def test_docker_entrypoint_runs_the_split_services(tmp_path):
    text = (REPO / 'infra' / 'docker' / 'entrypoint.sh').read_text()
    assert 'exec python manage.py run_scheduler' in text
    assert 'exec python manage.py db_worker --queue-name default' in text


def test_ops_health_of_the_background_services(monkeypatch):
    mod = ops_module()
    calls = []
    monkeypatch.setattr(mod, 'wait_db', lambda seconds: calls.append(seconds) or True)
    for svc in ('worker', 'scheduler', 'tasks'):
        monkeypatch.setenv('HAJIRI_SERVICE', svc)
        assert mod.health() == 0
    assert calls == [3, 3, 3]


def test_ops_has_user_and_has_company(db):
    from hajiri.core import auth
    mod = ops_module()
    cid = mod.ensure_company('ACME', 'Acme')
    auth.create_user(company_id=cid, email='Ops.Person@Acme.test', name='Ops', password='correct-horse-battery-staple', roles=['admin'])
    assert mod.has_user('OPS.person@acme.test') and not mod.has_user('nobody@acme.test')
    assert mod.has_company('ACME') and not mod.has_company('acme-not')


# ---------------------------------------------------------------- the web app's build (React, frontend/)

def test_vite_builds_into_the_folder_django_serves():
    cfg = (REPO / 'frontend' / 'vite.config.js').read_text()
    assert "'../server/frontend_build'" in cfg and 'emptyOutDir: true' in cfg
    from django.conf import settings
    assert Path(settings.FRONTEND_BUILD_DIR) == REPO / 'server' / 'frontend_build'
    assert Path(settings.WHITENOISE_ROOT) == Path(settings.FRONTEND_BUILD_DIR)


def test_build_script_runs_npm_ci_and_vite_build():
    text = (SCRIPTS / 'build-frontend.sh').read_text()
    assert 'npm ci' in text and 'npx vite build' in text and 'node_ok' in text
    assert '--no-install' in text


def test_update_rebuilds_only_with_node_and_changed_sources():
    text = (SCRIPTS / 'update.sh').read_text()
    assert 'git diff --quiet "$OLD_HEAD" HEAD -- frontend' in text
    assert 'git diff --quiet "$OLD_HEAD" HEAD -- server/frontend_build' in text
    assert 'elif node_ok; then\n  "$ROOT/scripts/build-frontend.sh"' in text
    # A local rebuild is reset before pulling, so `git pull --ff-only` is never blocked by build output.
    assert text.index('git checkout -- server/frontend_build') < text.index('git pull --ff-only')


def test_setup_and_start_check_the_committed_build():
    assert 'check_frontend_build' in (SCRIPTS / 'setup.sh').read_text()
    assert 'check_frontend_build' in (SCRIPTS / 'start.sh').read_text()


def test_docker_builds_the_web_app_in_a_node_stage():
    text = (REPO / 'infra' / 'docker' / 'Dockerfile').read_text()
    assert 'FROM node:22-bookworm-slim AS frontend' in text
    assert 'RUN npm ci' in text and 'npx vite build' in text
    assert 'COPY --from=frontend /build/server/frontend_build server/frontend_build' in text
    ignore = (REPO / 'infra' / 'docker' / 'Dockerfile.dockerignore').read_text().split()
    assert '**/node_modules' in ignore and 'server/frontend_build' in ignore


def test_node_ok_checks_the_vite_node_versions():
    text = (SCRIPTS / 'lib.sh').read_text()
    assert '(a===20&&b>=19)||(a===22&&b>=12)||a>=23' in text
    import shutil
    if shutil.which('node'):
        r = subprocess.run(['bash', '-c', f'source "{SCRIPTS / "lib.sh"}"; node_ok && echo yes || echo no'],
                           capture_output=True, text=True, timeout=30)
        major, minor = (int(x) for x in subprocess.run(['node', '-p', 'process.versions.node'], capture_output=True,
                                                         text=True).stdout.split('.')[:2])
        expected = (major == 20 and minor >= 19) or (major == 22 and minor >= 12) or major >= 23
        assert r.stdout.strip() == ('yes' if expected else 'no')
