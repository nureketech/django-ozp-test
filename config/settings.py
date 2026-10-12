"""
Жоба баптаулары.

Құпия мәндер (SECRET_KEY, DEBUG, ALLOWED_HOSTS, DATABASE_URL) `.env` файлынан алынады.
Үлгісі — `.env.example`.
"""

import os
from pathlib import Path

import dj_database_url
from django.core.exceptions import ImproperlyConfigured
from django.utils.translation import gettext_lazy as _
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent

# .env файлын оқу (файл болмаса — қате емес, әдепкі мәндер қолданылады)
load_dotenv(BASE_DIR / ".env")

DEBUG = os.environ.get("DEBUG", "False").lower() in ("1", "true", "yes")

SECRET_KEY = os.environ.get("SECRET_KEY", "")
if not SECRET_KEY:
    if DEBUG:
        # Тек жергілікті әзірлеуге арналған кілт
        SECRET_KEY = "django-insecure-local-development-only"
    else:
        raise ImproperlyConfigured("SECRET_KEY .env файлында көрсетілуі керек.")

ALLOWED_HOSTS = [
    host.strip()
    for host in os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
    if host.strip()
]

# Серверде (DEBUG=False): сайт тек HTTPS арқылы. Nginx сұранысты Gunicorn-ға
# жібергенде X-Forwarded-Proto тақырыбымен HTTPS екенін хабарлайды.
if not DEBUG:
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    # HSTS: браузер сайтты тек HTTPS арқылы ашады. HTTPS дұрыс жұмыс істейтініне
    # көз жеткізгеннен кейін ғана .env-те қосыңыз (мысалы, 31536000 — бір жыл)
    SECURE_HSTS_SECONDS = int(os.environ.get("SECURE_HSTS_SECONDS", "0"))

# Формалар жіберілетін домендер, мысалы https://test.example.kz (міндетті емес)
CSRF_TRUSTED_ORIGINS = [
    origin.strip()
    for origin in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",")
    if origin.strip()
]


INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    # Жобаның өз қосымшалары
    "apps.accounts",
    "apps.quiz",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    # Тілді cookie-ден (немесе браузерден) анықтайды
    "django.middleware.locale.LocaleMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.template.context_processors.i18n",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                # Тест жүріп жатса — навигацияда «Шығу» бұғатталады
                "apps.quiz.context_processors.attempt_in_progress",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"


# Деректер қоры: DATABASE_URL берілсе — PostgreSQL, әйтпесе жергілікті SQLite
DATABASE_URL = os.environ.get("DATABASE_URL") or f"sqlite:///{BASE_DIR / 'db.sqlite3'}"
DATABASES = {
    "default": dj_database_url.parse(DATABASE_URL, conn_max_age=600),
}


# Кіру/шығу: кіргеннен кейін рөлге қарай бағыттау accounts:after_login-де
LOGIN_URL = "accounts:login"
LOGIN_REDIRECT_URL = "accounts:after_login"
LOGOUT_REDIRECT_URL = "quiz:home"

# Бірінші — негізгі хэшер; AccessCodeHasher — тек импорттағы кіру кодтарына
# (студент бірінші кіргенде код негізгі хэшермен қайта сақталады)
PASSWORD_HASHERS = [
    "django.contrib.auth.hashers.PBKDF2PasswordHasher",
    "apps.accounts.hashers.AccessCodeHasher",
    "django.contrib.auth.hashers.PBKDF2SHA1PasswordHasher",
    "django.contrib.auth.hashers.Argon2PasswordHasher",
    "django.contrib.auth.hashers.BCryptSHA256PasswordHasher",
    "django.contrib.auth.hashers.ScryptPasswordHasher",
]

# Django-ның дайын тексерулері, хабарламалары қазақша (apps/accounts/validators.py)
AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "apps.accounts.validators.UserAttributeSimilarityValidator"},
    {"NAME": "apps.accounts.validators.MinimumLengthValidator"},
    {"NAME": "apps.accounts.validators.CommonPasswordValidator"},
    {"NAME": "apps.accounts.validators.NumericPasswordValidator"},
]


# Тілдер: негізгі тіл — қазақша, екінші — орысша
LANGUAGE_CODE = "kk"
LANGUAGES = [
    ("kk", _("Қазақша")),
    ("ru", _("Орысша")),
]
LOCALE_PATHS = [BASE_DIR / "locale"]
USE_I18N = True

# Таңдалған тіл cookie-де бір жыл сақталады
LANGUAGE_COOKIE_AGE = 60 * 60 * 24 * 365

TIME_ZONE = "Asia/Almaty"
USE_TZ = True


# Статика (CSS, JS) және жүктелген суреттер
STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

# Серверде (DEBUG=False) collectstatic файл атына хэш қосады (attempt.abc123.js):
# Nginx статиканы кэштейді, ал JS өзгерсе — аты да өзгеріп, браузер жаңасын жүктейді.
# Жергілікті әзірлеуде (және тесттерде) — қарапайым сақтау, collectstatic керек емес.
if not DEBUG:
    STORAGES = {
        "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
        "staticfiles": {
            "BACKEND": "django.contrib.staticfiles.storage.ManifestStaticFilesStorage"
        },
    }

MEDIA_URL = "media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"


# Журнал: ескертулер мен қателер консольге шығады (серверде — journalctl -u ozp-test).
# Мысалы, банкте сұрақ жетпегендегі ескерту (apps/quiz/services.py) осында көрінеді.
LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "simple": {"format": "{asctime} {levelname} {name}: {message}", "style": "{"},
    },
    "handlers": {
        "console": {"class": "logging.StreamHandler", "formatter": "simple"},
    },
    "root": {"handlers": ["console"], "level": "WARNING"},
}
