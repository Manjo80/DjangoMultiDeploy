📦 Django GPS Manager – Debian/LXC Installer

Automatisches Installationsskript für einen Django + Gunicorn + nginx + PostgreSQL Stack auf Debian 12 (optimiert für Proxmox LXC).

Das Skript richtet einen vollständigen Webserver ein, inkl.:
	•	App-User
	•	Python venv
	•	Django Projekt + App
	•	PostgreSQL (lokal oder remote)
	•	systemd-Service
	•	nginx Reverse-Proxy
	•	DEV/PROD-Modus
	•	Host- und Domain-Management
	•	Reverse-Proxy-Support (HTTPS extern)

⸻

✅ Features

🔧 Grundsetup
	•	Debian 12 kompatibel
	•	automatisches Paket-Setup
	•	eigener Linux-User für die App
	•	Python venv pro Projekt
	•	Gunicorn + systemd Service
	•	nginx Reverse-Proxy
	•	PostgreSQL lokal oder remote

⸻

🧩 DEV / PROD Modus

Bei der Installation wählbar:

DEV
	•	DEBUG=True
	•	gedacht für Entwicklung / LAN / Tests

PROD
	•	DEBUG=False
	•	saubere ALLOWED_HOSTS
	•	nginx server_name automatisch gesetzt
	•	vorbereitet für externen Reverse Proxy (HTTPS)

Der aktuelle Modus wird in .env gespeichert:

MODE=dev | prod
DEBUG=True | False


⸻

🔁 Modus später wechseln

Automatisch erzeugtes Tool:

sudo /usr/local/sbin/<projekt>_switch_mode.sh dev
sudo /usr/local/sbin/<projekt>_switch_mode.sh prod

Das Script:
	•	ändert .env
	•	passt nginx server_name an
	•	startet nginx + Django Service neu

⸻

🌍 Host / Domain Management

Automatisch erzeugtes Admin-Tool:

sudo /usr/local/sbin/<projekt>_set_hosts.sh

Anzeigen

sudo <projekt>_set_hosts.sh list

Host hinzufügen

sudo <projekt>_set_hosts.sh add gpsmgr.intern.lan

Externe Domain (Reverse Proxy, HTTPS)

sudo <projekt>_set_hosts.sh add gps.example.com --https

→ setzt automatisch:
	•	ALLOWED_HOSTS
	•	nginx server_name
	•	CSRF_TRUSTED_ORIGINS=https://...

Komplette Liste neu setzen

sudo <projekt>_set_hosts.sh set "192.168.1.10,gpsmgr.intern.lan,gps.example.com,localhost" --https

Danach wird automatisch neu gestartet:
	•	nginx
	•	Django Service

⸻

🌐 Reverse Proxy geeignet

Das Setup ist vorbereitet für:
	•	externes HTTPS (Firewall, HAProxy, nginx Proxy, Traefik, Cloudflare, etc.)
	•	korrektes X-Forwarded-Proto
	•	saubere CSRF-Freigaben

In PROD wird automatisch gesetzt:

SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")


⸻

🗄️ Datenbank

Beim Installieren wählbar:

Option 1 – lokal
	•	PostgreSQL wird installiert
	•	DB + User werden automatisch erstellt

Option 2 – remote
	•	verbindet sich per psql
	•	erstellt DB + User
	•	trägt Zugangsdaten in .env ein

⸻

🔐 Passwort- & Security-Handling

Wo liegen die Zugangsdaten?

Alle Secrets liegen in:

/srv/<projekt>/.env

Beispiel:

DB_NAME=gpsmgr
DB_USER=gpsmgr_user
DB_PASS=...
SECRET_KEY=...
ALLOWED_HOSTS=...
CSRF_TRUSTED_ORIGINS=...

Rechte werden automatisch gesetzt:

chmod 600 .env
chown <appuser>:<appuser> .env

➡ Nur root und der App-User können die Datei lesen.

⸻

Sind die DB-Passwörter „sicher“?

Kurzfassung:
Ja, für Serverbetrieb. Nein, für Git.

✔ liegen nicht im Code
✔ liegen nicht in systemd Units
✔ liegen nicht im Klartext im Skript
✔ Datei ist 600 geschützt
❌ dürfen niemals ins Git-Repo

Empfohlen:

.env
__pycache__/
.venv/

in .gitignore.

⸻

▶️ Installation

chmod +x install.sh
sudo ./install.sh

Das Skript fragt u. a. ab:
	•	Projektname → /srv/<name>
	•	DEV oder PROD
	•	ALLOWED_HOSTS (mit IP-Autodetect)
	•	Linux App-User
	•	DB-Modus (lokal/remote)
	•	DB-Passwörter
	•	Django SECRET_KEY

⸻

▶️ Nach der Installation

Superuser anlegen:

sudo -u <appuser> /srv/<projekt>/.venv/bin/python /srv/<projekt>/manage.py createsuperuser

Service prüfen:

systemctl status <projekt>

Web:

http://SERVER-IP/admin

(Extern dann über deinen Reverse Proxy)

⸻

⚙️ Service-Steuerung

systemctl start <projekt>
systemctl stop <projekt>
systemctl restart <projekt>
journalctl -u <projekt> -f


⸻

⚠️ Wichtige Hinweise

Proxmox LXC

Container muss systemd erlauben:

features: nesting=1,keyctl=1

Sicherheit

Der App-User ist absichtlich sudo-fähig.
Das ist für Projekt-/Lab-Betrieb praktisch, aber kein Enterprise-Hardening.

⸻

📌 Was dieses Skript bewusst NICHT macht
	•	kein HTTPS (macht dein externer Reverse Proxy)
	•	kein Firewall-Setup
	•	kein Backup-System
	•	kein Update-Automation

Das ist Absicht:
Das Skript ist ein stabiler technischer Unterbau, kein Hosting-Framework.

⸻

🧠 Gedacht für
	•	Proxmox / LXC
	•	interne Web-Tools
	•	Behörden-/Projekt-Systeme
	•	Reverse-Proxy-Umgebungen
	•	saubere reproduzierbare Server-Setups

⸻

Wenn du willst, schreibe ich dir noch zusätzlich:
	•	.gitignore passend zum Setup
	•	update.sh (git pull + migrate + restart)
	•	oder eine Kurz-Installationsanleitung nur für Nutzer deines Repos.


# 🔐 GitHub SSH-Login einrichten (Debian / LXC)

## ✅ 1. Als der richtige User einloggen

Nimm den User, der später `git clone` / `git pull` machen soll
(empfohlen: **nicht root**, sondern dein Admin-User oder App-User).

Beispiel:

```bash
su - deinuser
```

Prüfen:

```bash
whoami
```

---

## ✅ 2. SSH-Key erzeugen

```bash
ssh-keygen -t ed25519 -f ~/.ssh/github_ed25519 -C "github-$(hostname)"
```

→ 3× Enter
→ Datei: `~/.ssh/github_ed25519`

Rechte setzen (wichtig):

```bash
chmod 700 ~/.ssh
chmod 600 ~/.ssh/github_ed25519
chmod 644 ~/.ssh/github_ed25519.pub
```

---

## ✅ 3. SSH-Config erstellen (damit immer der richtige Key genutzt wird)

```bash
nano ~/.ssh/config
```

Inhalt:

```ssh
Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_ed25519
  IdentitiesOnly yes
```

Dann:

```bash
chmod 600 ~/.ssh/config
```

---

## ✅ 4. Public Key bei GitHub eintragen

Key anzeigen:

```bash
cat ~/.ssh/github_ed25519.pub
```

Auf GitHub:

➡ Settings
➡ SSH and GPG keys
➡ New SSH key
➡ Einfügen → Save

---

## ✅ 5. Verbindung testen (das ist der wichtigste Schritt)

```bash
ssh -T git@github.com
```

Erwartet:

```
Hi <deinName>! You've successfully authenticated...
```

Wenn das nicht kommt → nicht weitermachen.

---

## ✅ 6. Repo richtig clonen (SSH, nicht HTTPS)

❌ FALSCH:

```
https://github.com/Manjo80/GPSDB.git
```

✅ RICHTIG:

```
git@github.com:Manjo80/GPSDB.git
```

Clone:

```bash
cd /srv
git clone git@github.com:Manjo80/GPSDB.git
```

---

## ✅ 7. Später updaten (pull)

```bash
cd /srv/GPSDB
git pull
```

Fertig.

---

# ⚠️ Die 4 häufigsten Fehler (realistisch)

### ❌ Git fragt nach Username/Passwort

→ du nutzt HTTPS

Fix:

```bash
git remote set-url origin git@github.com:Manjo80/GPSDB.git
```

---

### ❌ „Repository not found“

→ entweder kein Zugriff auf privates Repo
→ oder falscher Account/Key

Check:

```bash
ssh -T git@github.com
```

---

### ❌ Falscher Key wird benutzt

Debug:

```bash
ssh -vT git@github.com
```

Du musst sehen:

```
Offering public key: /home/.../.ssh/github_ed25519
```

---

### ❌ Funktioniert als root, aber nicht als App-User

→ jeder Linux-User braucht **seinen eigenen Key**

SSH-Keys sind **usergebunden**, nicht systemweit.

---

# 🔐 Sauber für Server (empfohlen): Deploy Key statt Account-Key

Wenn der Server **nur 1 Repo** braucht:

GitHub Repo → Settings → Deploy keys → Add deploy key
✔ read only
✔ eigenen Key nur für dieses Repo

Das ist sicherer als dein persönlicher Account-Key.

---

# 🧪 Minimal-Checkliste

```bash
ssh -T git@github.com
git clone git@github.com:Manjo80/GPSDB.git
cd GPSDB
git pull
```

Wenn das durchläuft → Setup ist sauber.

---

Wenn du willst, mache ich dir als nächsten Schritt:

✔ ein `update.sh` (git pull + migrate + restart service)
✔ ein `first-deploy.sh`
✔ oder binde Git direkt in dein Install-Script ein.
