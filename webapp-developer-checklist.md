# DjangoMultiDeploy — Webapp-Entwickler Checkliste

> Was du in deinem Django-Projekt anpassen musst, damit es mit **DjangoMultiDeploy** kompatibel ist.

---

## 1. Pflicht: Projektstruktur

Dein Projekt muss diese Dateien im **Root-Verzeichnis** haben:

| Datei | Zweck |
|---|---|
| `manage.py` | Django-Einstiegspunkt — **muss im Root liegen** |
| `requirements.txt` | Alle Abhängigkeiten (inkl. `python-dotenv`) |
| `<modul>/wsgi.py` | Wird automatisch erkannt |
| `<modul>/settings.py` | Muss Werte aus `.env` lesen (siehe unten) |

---

## 2. Pflicht: `settings.py` — Umgebungsvariablen einlesen

Das Tool erzeugt automatisch eine `.env`-Datei. Deine `settings.py` **muss** diese Werte einlesen.

### Minimale kompatible Konfiguration:

```python
import os
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / '.env')

SECRET_KEY = os.getenv('SECRET_KEY')
DEBUG = os.getenv('DEBUG', 'False') == 'True'

ALLOWED_HOSTS = [h.strip() for h in os.getenv('ALLOWED_HOSTS', '').split(',') if h.strip()]

DATABASES = {
    'default': {
        'ENGINE':   os.getenv('DB_ENGINE', 'django.db.backends.sqlite3'),
        'NAME':     os.getenv('DB_NAME', BASE_DIR / 'db.sqlite3'),
        'USER':     os.getenv('DB_USER', ''),
        'PASSWORD': os.getenv('DB_PASSWORD', ''),
        'HOST':     os.getenv('DB_HOST', ''),
        'PORT':     os.getenv('DB_PORT', ''),
    }
}

STATIC_URL  = '/static/'
STATIC_ROOT = BASE_DIR / 'staticfiles'
MEDIA_URL   = '/media/'
MEDIA_ROOT  = BASE_DIR / 'media'
```

### Von der `.env` gesetzte Variablen (automatisch durch das Tool):

| Variable | Beispielwert | Beschreibung |
|---|---|---|
| `SECRET_KEY` | `abc123...` | Django Secret Key |
| `DEBUG` | `False` | `True` nur im DEV-Modus |
| `ALLOWED_HOSTS` | `example.com,192.168.1.10` | Kommasepariert |
| `MODE` | `dev` oder `prod` | Deployment-Modus |
| `DB_ENGINE` | `django.db.backends.postgresql` | Datenbanktyp |
| `DB_NAME` | `meinprojekt_db` | Datenbankname |
| `DB_USER` | `meinprojekt_user` | Datenbankbenutzer |
| `DB_PASSWORD` | `geheim` | Datenbankpasswort |
| `DB_HOST` | `localhost` | Datenbankhost |
| `DB_PORT` | `5432` | Datenbankport |
| `CSRF_TRUSTED_ORIGINS` | `https://example.com` | Automatisch aus ALLOWED_HOSTS |

---

## 3. Empfohlen: Reverse-Proxy Einstellungen (für PROD-Modus)

```python
# Für nginx / Zoraxy Reverse Proxy
USE_X_FORWARDED_HOST = os.getenv('USE_X_FORWARDED_HOST', 'False') == 'True'

SECURE_PROXY_SSL_HEADER = (
    ('HTTP_X_FORWARDED_PROTO', 'https')
    if os.getenv('MODE') == 'prod'
    else None
)

CSRF_TRUSTED_ORIGINS = [
    o.strip() for o in os.getenv('CSRF_TRUSTED_ORIGINS', '').split(',') if o.strip()
]
```

---

## 4. Was das Tool automatisch in deinem Projekt ändert

> Du musst das **nicht** selbst machen — das Tool erledigt es bei der Installation:

| Aktion | Details |
|---|---|
| Admin-URL umbenennen | `/admin/` → `/djadmin/` (in `urls.py`) |
| Superuser anlegen | `createsuperuser --noinput` mit gesetzten Zugangsdaten |
| Migrations ausführen | `python manage.py migrate` |
| Static files sammeln | `python manage.py collectstatic --noinput` |
| `.env` erzeugen | Mit allen Werten aus Tabelle oben |
| Health-Check erstellen | Nur bei leeren Projekten (nicht bei GitHub/ZIP) |

**Wichtig:** Wenn dein Projekt von **GitHub oder ZIP** kommt, wird der Health-Check **nicht** automatisch erstellt. Füge ihn selbst hinzu (siehe Abschnitt 5).

---

## 5. Empfohlen: Health-Check Endpoint

Der Manager und das Update-Skript prüfen `/health/` um zu sehen ob das Projekt läuft. Füge diesen Endpoint selbst hinzu:

### In deinem Haupt-App-Verzeichnis (z.B. `myapp/views.py`):

```python
from django.http import JsonResponse
from django.db import connection
import os

def health_check(request):
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    return JsonResponse({
        "status": "ok" if db_ok else "degraded",
        "database": "ok" if db_ok else "error",
        "mode": os.getenv("MODE", "unknown")
    })
```

### In `urls.py` registrieren:

```python
from django.urls import path
from . import views  # oder direkt importieren

urlpatterns = [
    # ... deine anderen URLs ...
    path('health/', views.health_check),
]
```

---

## 6. `requirements.txt` — Mindestinhalt

```
django>=4.2
python-dotenv>=1.0
gunicorn>=21.0
# ... deine weiteren Abhängigkeiten ...
```

> `gunicorn` und `python-dotenv` werden durch das Tool **zusätzlich installiert**, auch wenn sie fehlen. Trotzdem empfohlen, sie einzutragen.

---

## 7. Was du **nicht** in die ZIP packen sollst

| Ausschließen | Warum |
|---|---|
| `.env` | Wird durch das Tool erzeugt (enthält Secrets) |
| `.venv/` oder `venv/` | Wird neu angelegt |
| `staticfiles/` | Wird per `collectstatic` erzeugt |
| `media/` | User-Uploads — getrennt verwaltet |
| `__pycache__/` / `*.pyc` | Server-spezifisch, unnötig |
| `*.log` | Logs in `/var/log/<projekt>/` verwaltet |
| `db.sqlite3` | Wird frisch angelegt |

---

## 8. Unterstützte ZIP-Strukturen

Beide Formate werden automatisch erkannt:

**Flaches ZIP:**
```
manage.py
requirements.txt
myapp/
  settings.py
  urls.py
  wsgi.py
```

**GitHub-Style ZIP** (ein Top-Level-Verzeichnis):
```
myapp-main/
  manage.py
  requirements.txt
  myapp/
    settings.py
    urls.py
    wsgi.py
```

---

## 9. Admin-Zugang nach Installation

| Was | Wert |
|---|---|
| URL | `https://<domain>/djadmin/` (nicht `/admin/`) |
| Benutzer | Beim Setup angegeben |
| Passwort | Beim Setup angegeben |

---

## 10. Kurzcheck — Ist mein Projekt kompatibel?

- [ ] `manage.py` liegt im Root-Verzeichnis
- [ ] `requirements.txt` vorhanden (inkl. `python-dotenv`)
- [ ] `settings.py` liest `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `DATABASES` aus `.env`
- [ ] `STATIC_ROOT` und `MEDIA_ROOT` sind konfiguriert
- [ ] `/health/` Endpoint vorhanden (empfohlen)
- [ ] Kein `.env`, keine `.venv/`, kein `staticfiles/` in der ZIP
