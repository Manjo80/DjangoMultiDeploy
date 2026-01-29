# Django Debian/Ubuntu Installer (Gunicorn + nginx + PostgreSQL)

Automatisches Installationsskript für einen Django-Server auf **Debian oder Ubuntu**.

Enthält:
- Python venv
- Django Projekt + App
- PostgreSQL (lokal oder remote)
- Gunicorn + systemd Service
- nginx Reverse Proxy
- DEV / PROD Modus
- Reverse-Proxy-Support (HTTPS extern möglich)

---

## Features

- Debian 12 / Ubuntu 22.04+ kompatibel
- eigener App-User
- `.env`-basierte Konfiguration
- `ALLOWED_HOSTS` Abfrage + nginx `server_name`
- vorbereitet für externen Reverse Proxy
- DB automatisch anlegen (lokal oder remote)

---

## Installation

```bash
chmod +x install.sh
sudo ./install.sh

Abgefragt werden u. a.:
	•	Projektname → /srv/<name>
	•	DEV oder PROD
	•	ALLOWED_HOSTS (IP wird automatisch erkannt)
	•	Linux App-User
	•	DB Modus (lokal/remote)
	•	DB-Passwörter
	•	Django SECRET_KEY

⸻

Nach der Installation

Superuser anlegen:

sudo -u <appuser> /srv/<projekt>/.venv/bin/python /srv/<projekt>/manage.py createsuperuser

Service prüfen:

systemctl status <projekt>

Aufruf (intern):

http://SERVER-IP/admin

Extern über deinen Reverse Proxy (HTTPS).

⸻

Daten & Sicherheit

Alle Secrets liegen in:

/srv/<projekt>/.env

Rechte:

chmod 600 .env
chown <appuser>:<appuser> .env

✔ nicht im Code
✔ nicht im systemd Service
✔ nur root + App-User
❌ darf nicht ins Git-Repo

Empfohlen für .gitignore:

.env
.venv/
__pycache__/


⸻

Reverse Proxy (externes HTTPS)

Das Setup ist vorbereitet für:
	•	HAProxy
	•	nginx Proxy
	•	Traefik
	•	Cloudflare Tunnel

In PROD wird automatisch gesetzt:

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")

Bei externen Domains ggf. zusätzlich setzen:

CSRF_TRUSTED_ORIGINS=https://deinedomain.tld

(in .env)

⸻

Proxmox LXC Hinweis

Container braucht:

nesting=1
keyctl=1


⸻

Was das Skript bewusst nicht macht
	•	kein HTTPS (macht dein Proxy)
	•	kein Firewall-Setup
	•	kein Backup-System
	•	kein Update-Automatismus

⸻

Gedacht für
	•	Proxmox / LXC
	•	interne Web-Tools
	•	Projekt- & Behörden-Systeme
	•	reproduzierbare Server-Setups

---

# ✅ 3) GitHub SSH-Anleitung (für dein README oder Wiki)

```markdown
## GitHub SSH Zugriff einrichten (für private Repos)

### 1. Als Ziel-User einloggen
```bash
su - <user>

2. SSH-Key erzeugen

ssh-keygen -t ed25519 -f ~/.ssh/github_ed25519 -C "github-$(hostname)"

Rechte:

chmod 700 ~/.ssh
chmod 600 ~/.ssh/github_ed25519
chmod 644 ~/.ssh/github_ed25519.pub

3. SSH Config anlegen

nano ~/.ssh/config

Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_ed25519
  IdentitiesOnly yes

chmod 600 ~/.ssh/config

4. Public Key bei GitHub eintragen

cat ~/.ssh/github_ed25519.pub

GitHub → Settings → SSH and GPG keys → New SSH key

⸻

5. Testen

ssh -T git@github.com

Muss „authenticated“ melden.

⸻

6. Repo clonen

git clone git@github.com:DEINUSER/DEINREPO.git

7. Updates

git pull


⸻

Hinweis

SSH-Keys sind userbezogen.
Root, App-User, Admin-User → jeweils eigener Key nötig.

---