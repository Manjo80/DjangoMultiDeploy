# ✅ Kompatibilitäts-Checkliste für Webapps (DjangoMultiDeploy)

Damit sich dein Django-Projekt mit **DjangoMultiDeploy** (Installer + Web-Manager)
sauber installieren, updaten, sichern und scannen lässt, muss es ein paar
Konventionen erfüllen. Diese Datei fasst **alles** zusammen — als Vorlage für
dein GitHub-Template.

> Kurzfassung: Projekt-Root mit `manage.py`, `requirements.txt` und `wsgi.py`;
> **alle** Einstellungen aus der `.env` lesen (via `python-dotenv`); Migrationen
> committen; `STATIC_ROOT`/`MEDIA_ROOT` setzen; hinter Reverse Proxy die
> Proxy-/CSRF-Header konfigurieren; **eigene Pflicht-Env-Variablen** entweder mit
> Default versehen **oder** vor dem ersten Start in die `.env` eintragen.

---

## 1. Projektstruktur (Pflicht)

Das Tool sucht diese Dateien im **obersten Verzeichnis** des Repos:

| Datei | Zweck |
|---|---|
| `manage.py` | Muss im Projekt-Root liegen |
| `requirements.txt` | Alle Python-Abhängigkeiten (siehe §2) |
| `wsgi.py` (z. B. `core/wsgi.py`) | Wird für Gunicorn genutzt; das Django-Settings-Modul wird darüber erkannt |
| `settings.py` | Liest alle Werte aus der `.env` (siehe §4) |

Beispiel (Standard-Django-Layout):

```
repo/
├── manage.py
├── requirements.txt
├── core/
│   ├── __init__.py
│   ├── settings.py
│   ├── urls.py
│   └── wsgi.py
└── app/                # deine App(s)
    ├── models.py
    ├── views.py
    └── migrations/     # committen! (siehe §6)
```

---

## 2. `requirements.txt`

- **Alle** Laufzeit-Abhängigkeiten auflisten (am besten mit Versions-Pinning).
- `gunicorn` und `python-dotenv` werden vom Tool **immer zusätzlich** installiert —
  du kannst sie auch selbst eintragen, musst aber nicht.
- Für PostgreSQL/MySQL den passenden Treiber aufnehmen, z. B. `psycopg[binary]`
  (PostgreSQL) bzw. `mysqlclient` (MySQL) — je nachdem, welche DB du wählst.

---

## 3. Umgebungsvariablen, die das Tool bereitstellt

Das Tool erzeugt beim Installieren automatisch eine `.env` (chmod 600) im
Projekt-Root und trägt diese Schlüssel ein. **Deine `settings.py` muss genau
diese Namen lesen:**

| Variable | Bedeutung | Beispiel |
|---|---|---|
| `SECRET_KEY` | Django Secret Key (leer im Formular ⇒ wird sicher generiert) | `xx... (64+ Zeichen)` |
| `DEBUG` | `True`/`False` (String!) | `False` |
| `MODE` | `dev` oder `prod` | `prod` |
| `ALLOWED_HOSTS` | **kommasepariert** | `example.com,127.0.0.1` |
| `CSRF_TRUSTED_ORIGINS` | **kommasepariert**, mit Schema | `https://example.com` |
| `DB_ENGINE` | Django-DB-Backend | `django.db.backends.postgresql` |
| `DB_NAME` | Datenbankname | `myapp` |
| `DB_USER` | Datenbank-User | `myapp_user` |
| `DB_PASS` | Datenbank-Passwort ⚠️ **`DB_PASS`, nicht `DB_PASSWORD`** | `…` |
| `DB_HOST` | DB-Host | `localhost` |
| `DB_PORT` | DB-Port | `5432` |
| `LANGUAGE_CODE` | Sprache | `de-de` |
| `TIME_ZONE` | Zeitzone | `Europe/Berlin` |
| `EMAIL_HOST` u. a. | SMTP (optional) | leer = Console-Backend |
| `PROJECTNAME` | interner Projektname | `myapp` |

> ⚠️ Häufige Falle: Der DB-Passwort-Schlüssel heißt **`DB_PASS`** (nicht
> `DB_PASSWORD`). Wer `os.getenv("DB_PASSWORD")` nutzt, bekommt eine leere DB-Auth.

---

## 4. `settings.py` — Muster, das direkt funktioniert

Wichtig: Die `.env` **selbst laden** (mit `python-dotenv`). Grund: Management-
Befehle wie `migrate`/`collectstatic` laufen **nicht** über systemd und haben die
`EnvironmentFile`-Variablen daher nicht automatisch — ohne `load_dotenv()`
scheitern sie.

```python
from pathlib import Path
import os
from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent.parent
load_dotenv(BASE_DIR / ".env")          # PFLICHT: .env selbst laden

def env_list(name: str):
    return [x.strip() for x in os.getenv(name, "").split(",") if x.strip()]

MODE       = os.getenv("MODE", "dev").lower()
SECRET_KEY = os.getenv("SECRET_KEY")     # kommt aus der .env
DEBUG      = os.getenv("DEBUG", "False") == "True"

ALLOWED_HOSTS        = env_list("ALLOWED_HOSTS")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")

DATABASES = {
    "default": {
        "ENGINE":   os.getenv("DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME":     os.getenv("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        "USER":     os.getenv("DB_USER", ""),
        "PASSWORD": os.getenv("DB_PASS", ""),   # DB_PASS!
        "HOST":     os.getenv("DB_HOST", ""),
        "PORT":     os.getenv("DB_PORT", ""),
    }
}

# Statische Dateien & Medien (siehe §5 — Pfade müssen so heißen)
STATIC_URL  = "/static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_URL   = "/media/"
MEDIA_ROOT  = BASE_DIR / "media"

# Hinter Reverse Proxy (nginx/Zoraxy/Cloudflare) im PROD-Modus (siehe §7)
if MODE == "prod":
    USE_X_FORWARDED_HOST    = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    CSRF_COOKIE_SECURE      = True
    SESSION_COOKIE_SECURE   = True
```

---

## 5. Statische Dateien & Medien (Pflicht)

- `STATIC_ROOT = BASE_DIR / "staticfiles"` und `MEDIA_ROOT = BASE_DIR / "media"` —
  **genau diese Ordnernamen**, denn nginx liefert `/static/` aus
  `<projekt>/staticfiles/` und `/media/` aus `<projekt>/media/`.
- `python manage.py collectstatic --noinput` muss fehlerfrei laufen.
- `staticfiles/` und `media/` **nicht** ins Repo committen (siehe §9).
- Beim Update sammelt das Tool `collectstatic` automatisch ein.

---

## 6. Migrationen (Pflicht)

- **Migrationsdateien committen** (`app/migrations/*.py`). Sie sind Code und
  gehören in die Versionskontrolle.
- Auf dem Server wird **nur** `migrate` ausgeführt — **niemals** automatisch
  `makemigrations`. Das Update warnt bei „Modelländerungen ohne Migration", führt
  aber keine Generierung durch. Fehlt eine Migration, bleibt das Schema veraltet.
- Ablauf lokal: `makemigrations` → committen → deployen → `migrate` läuft am Server.

---

## 7. Reverse Proxy, HTTPS & CSRF

Die App läuft hinter nginx (und ggf. weiteren Proxies). Damit Login/CSRF und
Redirects funktionieren:

- `CSRF_TRUSTED_ORIGINS` **muss** die öffentliche Origin mit Schema enthalten,
  z. B. `https://example.com` (liefert das Tool aus der `.env`).
- `SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")` setzen, sonst
  hält Django die Verbindung für HTTP.
- HSTS **nur an einer Stelle** setzen (App **oder** Proxy/Cloudflare), nicht
  doppelt — sonst „Strict-Transport-Security Multiple Header Entries".
- `SESSION_COOKIE_SECURE`/`CSRF_COOKIE_SECURE = True` im PROD-Modus.

---

## 8. Eigene / zusätzliche Umgebungsvariablen ⚠️ (wichtigste Lektion)

Braucht deine App **eigene** Secrets/Configs (z. B. `EVENT_ENCRYPTION_KEY`,
API-Keys, Feature-Flags), kennt das Tool diese **nicht** und trägt sie **nicht**
in die `.env` ein. Zwei saubere Wege:

**A) Pflicht-Variable mit hartem Zugriff** (`os.environ["X"]`) — dann **musst du
sie vor dem ersten Start in die `.env` eintragen**, sonst crasht bereits
`manage.py` beim Import der Settings:

```
# im Web-Manager: Projektdetail → „.env bearbeiten“, oder auf dem Server:
echo 'EVENT_ENCRYPTION_KEY=<wert>' | sudo tee -a /srv/<projekt>/.env
sudo systemctl restart <projekt>
```

Fernet-Key erzeugen (falls es ein Verschlüsselungs-Key ist):
```
/srv/<projekt>/.venv/bin/python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```
⚠️ Krypto-Keys nur **einmal** erzeugen und behalten — sonst sind bereits
verschlüsselte Daten unlesbar.

**B) Optionale Variable mit Default** — damit die App auch ohne Eintrag startet:

```python
FEATURE_X = os.getenv("FEATURE_X", "off")        # startet immer
API_TIMEOUT = int(os.getenv("API_TIMEOUT", "30"))
```

**Empfehlung:** Dokumentiere alle benötigten Zusatz-Variablen in deiner README
(Name, ob Pflicht, Beispielwert), damit du sie beim Anlegen direkt in die `.env`
schreiben kannst.

---

## 9. `.gitignore` (Pflicht)

Nicht ins Repo:

```
.env
.venv/
venv/
__pycache__/
*.py[cod]
staticfiles/
media/
*.log
db.sqlite3
```

Die `.env` wird pro Deployment erzeugt und enthält Secrets — niemals committen.

---

## 10. Admin-URL

Das Tool benennt `/admin/` in `/djadmin/` um (in `urls.py`). Wenn du eigene
Links/Tests auf den Admin setzt, nutze `/djadmin/` bzw. `reverse("admin:index")`
statt eines fest verdrahteten `/admin/`.

---

## 11. Optional: eigene Update-Schritte

Im Web-Manager (Projektdetail → *Update & Backup → Eigene Update-Befehle*) kannst
du zusätzliche `manage.py`-Kommandos hinterlegen, die beim Update automatisch nach
`migrate`/`collectstatic` laufen (z. B. `loaddata seed.json`, `clearsessions`).
Praktisch, wenn deine App eigene Deploy-Schritte braucht — kein Code-Zwang.

---

## 11b. ASGI / WebSockets / Django Channels

Das Tool kann das Projekt wahlweise als **WSGI** (Standard) oder **ASGI**
betreiben (Server-Typ im Install-Formular bzw. `SERVER_TYPE=asgi`). Im
ASGI-Modus läuft die App unter **Gunicorn mit Uvicorn-Worker**
(`<modul>.asgi:application -k uvicorn.workers.UvicornWorker`), und nginx wird
WebSocket-fähig konfiguriert (`Upgrade`/`Connection`-Header, langer
`proxy_read_timeout`).

Damit ASGI/Channels sauber läuft, muss dein Projekt:

- eine **`asgi.py`** im Modul-Verzeichnis mitbringen (bei Channels mit
  `ProtocolTypeRouter`/`URLRouter`). Fehlt sie, erzeugt das Tool eine
  Standard-`asgi.py` (nur HTTP, kein WebSocket-Routing).
- **`channels`** (und für Produktion **`channels-redis`**) in
  `requirements.txt` listen. Steht dort `channels`/`daphne`/`uvicorn`, schaltet
  das Tool **automatisch** in den ASGI-Modus.
- einen **Channel Layer** konfigurieren. In-Memory funktioniert nur
  single-worker/zum Testen; für mehrere Worker/Prozesse brauchst du **Redis**:

  ```python
  CHANNEL_LAYERS = {
      "default": {
          "BACKEND": "channels_redis.core.RedisChannelLayer",
          "CONFIG": {"hosts": [("127.0.0.1", 6379)]},
      }
  }
  ```
  Redis ist **nicht** Teil des Tools — bei Bedarf separat installieren
  (`apt install redis-server`).

> Beispiel `asgi.py` mit Channels:
> ```python
> import os
> from django.core.asgi import get_asgi_application
> os.environ.setdefault("DJANGO_SETTINGS_MODULE", "core.settings")
> django_asgi_app = get_asgi_application()
>
> from channels.routing import ProtocolTypeRouter, URLRouter
> from channels.auth import AuthMiddlewareStack
> import myapp.routing
> application = ProtocolTypeRouter({
>     "http": django_asgi_app,
>     "websocket": AuthMiddlewareStack(URLRouter(myapp.routing.websocket_urlpatterns)),
> })
> ```

---

## 12. ZIP-Deployment (Alternative zu GitHub)

Beide Strukturen werden erkannt (auch der GitHub-Button „Code → Download ZIP"):

```
# Flach                     # GitHub-Style (ein Top-Level-Ordner)
manage.py                   myapp-main/
requirements.txt              manage.py
core/                         requirements.txt
  settings.py                 core/ …
  wsgi.py
```

Nicht in die ZIP: `.env`, `.venv/`, `staticfiles/`, `media/`, `__pycache__/`, `*.log`.
Bei einem ZIP-**Update** werden `.env`, `.venv/`, `media/`, `staticfiles/` nie
überschrieben.

---

## 13. Betrieb & Health

- Die App sollte unter `/` eine Antwort liefern (die Dienst-Überwachung und
  Scanner prüfen Erreichbarkeit). Ein Redirect auf `/login/` ist okay.
- Fehler landen (im generierten Setup) unter `/var/log/<projekt>/django.log`.
  Wenn du eigenes Logging konfigurierst, halte es tolerant (Verzeichnis kann vom
  Tool angelegt werden).

---

## ✔️ Schnell-Checkliste vor dem ersten Deploy

- [ ] `manage.py`, `requirements.txt`, `wsgi.py` im Projekt-Root
- [ ] `settings.py` lädt die `.env` mit `load_dotenv(BASE_DIR / ".env")`
- [ ] `SECRET_KEY`, `DEBUG`, `ALLOWED_HOSTS`, `CSRF_TRUSTED_ORIGINS` aus der `.env`
- [ ] DB aus `DB_ENGINE/DB_NAME/DB_USER/**DB_PASS**/DB_HOST/DB_PORT`
- [ ] `STATIC_ROOT = BASE_DIR/"staticfiles"`, `MEDIA_ROOT = BASE_DIR/"media"`
- [ ] `collectstatic --noinput` läuft fehlerfrei
- [ ] Alle Migrationen committet; kein Verlass auf `makemigrations` am Server
- [ ] `SECURE_PROXY_SSL_HEADER` + Secure-Cookies im PROD-Modus gesetzt
- [ ] **Eigene Pflicht-Env-Variablen** haben einen Default **oder** stehen vor dem
      Start in der `.env` (z. B. `EVENT_ENCRYPTION_KEY`)
- [ ] `.gitignore` schließt `.env`, `.venv/`, `staticfiles/`, `media/`, Logs aus
- [ ] DB-Treiber (`psycopg[binary]` / `mysqlclient`) in `requirements.txt`, falls
      nicht SQLite
