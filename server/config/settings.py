"""
Django settings for Hajiri.

Reads the same environment as the TypeScript API (apps/api/.env) so both can run against one database during the
migration. DATA_ENCRYPTION_KEY and PASSWORD_PEPPER must stay the same: they decrypt stored PAN, bank and ID numbers
and verify existing password hashes.
"""
import os
from pathlib import Path
from urllib.parse import unquote, urlparse

from django.core.exceptions import ImproperlyConfigured
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
REPO_DIR = BASE_DIR.parent

# server/.env wins; apps/api/.env is the fallback while both stacks exist.
load_dotenv(BASE_DIR / '.env')
load_dotenv(REPO_DIR / 'apps' / 'api' / '.env')


def env(name: str, default: str | None = None) -> str:
    value = os.environ.get(name, default)
    if value is None:
        raise ImproperlyConfigured(f'{name} is not set (server/.env or apps/api/.env)')
    return value


def env_bool(name: str, default: bool) -> bool:
    return env(name, 'true' if default else 'false').lower() == 'true'


def database_from_url(url: str) -> dict:
    u = urlparse(url)
    if u.scheme not in ('postgres', 'postgresql'):
        raise ImproperlyConfigured(f'DATABASE_URL must be a postgres:// URL, got {u.scheme}://')
    return {
        'ENGINE': 'django.db.backends.postgresql',
        'NAME': u.path.lstrip('/'),
        'USER': unquote(u.username or ''),
        'PASSWORD': unquote(u.password or ''),
        'HOST': u.hostname or 'localhost',
        'PORT': str(u.port or 5432),
        'CONN_MAX_AGE': 60,
        'CONN_HEALTH_CHECKS': True,
        'OPTIONS': {'options': '-c TimeZone=UTC'},
    }


# Django's own signing key. Falls back to the pepper so a single generated secret is enough on existing installs.
SECRET_KEY = env('DJANGO_SECRET_KEY', os.environ.get('PASSWORD_PEPPER') or 'dev-only-insecure-key')
DEBUG = env_bool('DJANGO_DEBUG', False)
ALLOWED_HOSTS = [h for h in env('ALLOWED_HOSTS', 'localhost,127.0.0.1').split(',') if h]

# Field encryption and password hashing secrets shared with the TS API. Checked when first used, not at import, so
# migrations and tests run before secrets are configured (as loadDatabaseUrl does in the TS API).
DATA_ENCRYPTION_KEY = os.environ.get('DATA_ENCRYPTION_KEY', '')
PASSWORD_PEPPER = os.environ.get('PASSWORD_PEPPER', '')
RUN_WORKER = env_bool('RUN_WORKER', True)
SMTP_URL = os.environ.get('SMTP_URL', '')
SMS_WEBHOOK_URL = os.environ.get('SMS_WEBHOOK_URL', '')
# Other origins (comma separated) allowed to call the API with credentials, as the TS CORS_ORIGINS setting did.
CORS_ORIGINS = os.environ.get('CORS_ORIGINS', '')
# Page form posts from those origins (and any listed here) pass Django's CSRF origin check.
CSRF_TRUSTED_ORIGINS = sorted({o.strip() for o in (CORS_ORIGINS + ',' + os.environ.get('CSRF_TRUSTED_ORIGINS', '')).split(',')
                               if o.strip()})
# Behind an HTTPS reverse proxy that sets X-Forwarded-Proto.
if env_bool('TRUST_X_FORWARDED_PROTO', False):
    SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.postgres',
    'django.contrib.staticfiles',
    'hajiri.db',
    'hajiri.web',
    'hajiri.files',
    'hajiri.notifications',
    'hajiri.features',
    'hajiri.company_settings',
    'hajiri.reports',
    'hajiri.attendance',
    'hajiri.worker',
    'hajiri.integrations',
    'hajiri.workflow',
    'hajiri.gateway',
    'hajiri.devices',
    'hajiri.dashboard',
    'hajiri.leave',
    'hajiri.people',
    'hajiri.employees',
    'hajiri.lifecycle',
    'hajiri.shifts',
    'hajiri.regularisation',
    'hajiri.documents',
    'hajiri.hiring',
    'hajiri.careers',
    'hajiri.tasks',
    'hajiri.timesheets',
    'hajiri.helpdesk',
    'hajiri.engage',
    'hajiri.expenses',
    'hajiri.performance',
    'hajiri.learning',
    'hajiri.insights',
    'hajiri.relations',
    'hajiri.ot',
    'hajiri.attendance_tools',
    'hajiri.punch',
    'hajiri.field',
    'hajiri.kiosk',
    'hajiri.payroll',
    'hajiri.home',
    'demo',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'hajiri.web.middleware.SecurityHeadersMiddleware',
    # Serves /static/ itself (inside the security headers), so a single-machine install needs no web server in front.
    'hajiri.web.middleware.StaticFilesMiddleware',
    'django.middleware.common.CommonMiddleware',
    'hajiri.web.middleware.BearerCsrfMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
    'hajiri.web.middleware.AuthMiddleware',
    'hajiri.web.middleware.FlashMiddleware',
    'hajiri.web.middleware.HttpErrorMiddleware',
]
# A refused form post answers like the TS sameOriginGuard ("cross-site request refused"), as a page, toast or JSON.
CSRF_FAILURE_VIEW = 'hajiri.web.middleware.csrf_failure'
CSRF_COOKIE_SAMESITE = 'Lax'
X_FRAME_OPTIONS = 'DENY'

ROOT_URLCONF = env('DJANGO_ROOT_URLCONF', 'config.urls')

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [BASE_DIR / 'templates'],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.request',
                'django.template.context_processors.csrf',
                'hajiri.web.context_processors.layout',
            ],
            'builtins': ['hajiri.web.templatetags.hj'],
        },
    },
]

WSGI_APPLICATION = 'config.wsgi.application'

DATABASES = {'default': database_from_url(env('DATABASE_URL', 'postgres://localhost:5433/hajiri'))}
# Not DATABASE_URL_TEST: the test runner drops and recreates this database, and that one belongs to the TS suite.
DATABASES['default']['TEST'] = {'NAME': env('DJANGO_TEST_DATABASE', 'hajiri_django_test')}

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

LANGUAGE_CODE = 'en'
# Stored timestamps are UTC; business dates are Nepal dates (UTC+5:45) and shown in Bikram Sambat.
TIME_ZONE = 'Asia/Kathmandu'
USE_I18N = True
USE_TZ = True

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
STATICFILES_DIRS = [BASE_DIR / 'static']
# Until collectstatic has run (development, tests), WhiteNoise reads the files straight from STATICFILES_DIRS.
WHITENOISE_USE_FINDERS = env_bool('WHITENOISE_USE_FINDERS', True)
WHITENOISE_MAX_AGE = 3600

SESSION_COOKIE_SECURE = CSRF_COOKIE_SECURE = env_bool('COOKIE_SECURE', False)
# HTTPS-only installs: redirect plain HTTP (the /health probe is exempt) and send HSTS. Off by default because most
# installs run on a LAN over plain HTTP, as the TS server did.
SECURE_SSL_REDIRECT = env_bool('SECURE_SSL_REDIRECT', False)
SECURE_REDIRECT_EXEMPT = [r'^health$']
SECURE_HSTS_SECONDS = int(env('SECURE_HSTS_SECONDS', '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = env_bool('SECURE_HSTS_INCLUDE_SUBDOMAINS', False)
SECURE_HSTS_PRELOAD = env_bool('SECURE_HSTS_PRELOAD', False)
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

_LOG_LEVEL = {'trace': 'DEBUG', 'fatal': 'CRITICAL'}.get(env('LOG_LEVEL', 'info'), env('LOG_LEVEL', 'info').upper())
LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'handlers': {'console': {'class': 'logging.StreamHandler'}},
    'root': {'handlers': ['console'], 'level': _LOG_LEVEL},
}
