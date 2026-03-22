import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env', override=True)

SECRET_KEY = os.getenv('SECRET_KEY', 'change-me-in-production')
DEBUG = os.getenv('DEBUG', 'False') == 'True'
ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '*').split(',') if h.strip()]
CSRF_TRUSTED_ORIGINS = [h.strip() for h in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if h.strip()]

# Hinter nginx (Port 80): echten Host-Header übernehmen.
# Wird aus .env gesteuert — beim Direktzugriff auf Port 8888 MUSS dies False sein,
# sonst liefert Django 400/500 wegen fehlendem X-Forwarded-Host.
USE_X_FORWARDED_HOST = os.getenv('USE_X_FORWARDED_HOST', 'False') == 'True'
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https')

INSTALLED_APPS = [
    'django.contrib.contenttypes',
    'django.contrib.staticfiles',
    'django.contrib.auth',
    'django.contrib.sessions',
    'django.contrib.messages',
    'control',
]

MIDDLEWARE = [
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'control.middleware.SecurityHeadersMiddleware',  # CSP, Permissions-Policy, COEP, CORP
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'control.middleware.TwoFactorMiddleware',      # 2FA enforcement
    'control.middleware.IPWhitelistMiddleware',     # IP whitelist
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
]

ROOT_URLCONF = 'djmanager.urls'

TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
                'control.context_processors.csp_nonce',
            ],
        },
    },
]

WSGI_APPLICATION = 'djmanager.wsgi.application'

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': BASE_DIR / 'djmanager.db',
    }
}

STATIC_URL = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
_STATIC_SRC = BASE_DIR / 'static'
STATICFILES_DIRS = [_STATIC_SRC] if _STATIC_SRC.exists() else []
# WhiteNoise: serve static files directly from STATICFILES_DIRS without
# needing a separate collectstatic run (uses Django's staticfiles finders).
WHITENOISE_USE_FINDERS = True

DEFAULT_AUTO_FIELD = 'django.db.models.BigAutoField'

# ── Auth & Session ────────────────────────────────────────────────────────────

LOGIN_URL = '/login/'
LOGIN_REDIRECT_URL = '/dashboard/'
LOGOUT_REDIRECT_URL = '/login/'

# Session expires after inactivity; absolute timeout handled per-session in login view
SESSION_COOKIE_AGE = int(os.getenv('SESSION_TIMEOUT_SECONDS', str(8 * 3600)))  # default 8h
SESSION_SAVE_EVERY_REQUEST = True   # reset timer on each request
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = 'Lax'

# CSRF-Cookie langlebig machen (1 Jahr) — verhindert 403-Fehler wenn
# iOS/Android den Browser-Tab einfriert und Session-Cookies löscht
CSRF_COOKIE_AGE = 31449600  # 1 Jahr
# HttpOnly: JS kann den Token nicht mehr per document.cookie lesen.
# Alle AJAX-Requests nutzen {{ csrf_token }} aus dem Template-Kontext.
CSRF_COOKIE_HTTPONLY = True

# ── Security headers (relevant even behind reverse proxy) ─────────────────────
# CSP, Permissions-Policy, COEP, CORP are set by SecurityHeadersMiddleware
# (control/middleware.py) so they are present even when nginx lacks them.
# Django's SecurityMiddleware handles: HSTS, X-Content-Type-Options, X-XSS-Protection.
# Django's XFrameOptionsMiddleware handles: X-Frame-Options = DENY.

# ── Password validation ───────────────────────────────────────────────────────

AUTH_PASSWORD_VALIDATORS = [
    {'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator'},
    {'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
     'OPTIONS': {'min_length': 10}},
    {'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator'},
    {'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator'},
]

# ── Security (hinter nginx/SSL-Proxy) ─────────────────────────────────────────

SESSION_COOKIE_SECURE  = os.getenv('SESSION_COOKIE_SECURE',  'False') == 'True'
CSRF_COOKIE_SECURE     = os.getenv('CSRF_COOKIE_SECURE',     'False') == 'True'
SECURE_HSTS_SECONDS    = int(os.getenv('SECURE_HSTS_SECONDS', '0'))
SECURE_HSTS_INCLUDE_SUBDOMAINS = os.getenv('SECURE_HSTS_INCLUDE_SUBDOMAINS', 'False') == 'True'
SECURE_HSTS_PRELOAD    = os.getenv('SECURE_HSTS_PRELOAD',    'False') == 'True'
# Standardmäßig aktiviert; kann per .env überschrieben werden (z.B. für Tests)
SECURE_CONTENT_TYPE_NOSNIFF = os.getenv('SECURE_CONTENT_TYPE_NOSNIFF', 'True') == 'True'
X_FRAME_OPTIONS        = os.getenv('X_FRAME_OPTIONS', 'DENY')

# W008: SSL-Redirect übernimmt nginx — Django soll es nicht zusätzlich tun
# W021: HSTS Preload-List-Eintrag ist ein manueller Schritt, nicht automatisch
SILENCED_SYSTEM_CHECKS = ['security.W008', 'security.W021']

# ── Manager-specific ──────────────────────────────────────────────────────────

INSTALL_SCRIPT  = os.getenv('INSTALL_SCRIPT',  '/opt/DjangoMultiDeploy/Installv2.sh')
REGISTRY_DIR    = os.getenv('REGISTRY_DIR',    '/etc/django-servers.d')
INSTALL_LOG_DIR = os.getenv('INSTALL_LOG_DIR', '/tmp/djmanager_logs')
MANAGER_VENV    = os.getenv('MANAGER_VENV',    '/srv/djmanager/venv')

os.makedirs(INSTALL_LOG_DIR, exist_ok=True)

# ── Logging ───────────────────────────────────────────────────────────────────
_LOG_DIR = '/var/log/djmanager'
os.makedirs(_LOG_DIR, exist_ok=True)

LOGGING = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'verbose': {
            'format': '{asctime} {levelname} {name} {message}',
            'style': '{',
        },
    },
    'handlers': {
        'file': {
            'class': 'logging.FileHandler',
            'filename': os.path.join(_LOG_DIR, 'error.log'),
            'formatter': 'verbose',
            'level': 'WARNING',
        },
        'console': {
            'class': 'logging.StreamHandler',
            'formatter': 'verbose',
            'level': 'DEBUG',
        },
    },
    'root': {
        'handlers': ['file', 'console'],
        'level': 'WARNING',
    },
    'loggers': {
        'django': {
            'handlers': ['file', 'console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'django.request': {
            'handlers': ['file', 'console'],
            'level': 'WARNING',    # WARNING statt ERROR → 404/403 werden auch geloggt
            'propagate': False,
        },
        'djmanager.views': {
            'handlers': ['file', 'console'],
            'level': 'WARNING',
            'propagate': False,
        },
        'djmanager.scanner': {
            'handlers': ['file', 'console'],
            'level': 'INFO',
            'propagate': False,
        },
    },
}
