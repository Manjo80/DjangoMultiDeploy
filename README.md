🚀 Django Installer (Debian & Ubuntu)

Ein interaktives Installationsskript für einen Django-Server auf Debian oder Ubuntu, inkl.:
	•	Python venv (projektgebunden)
	•	Django + Gunicorn
	•	PostgreSQL (lokal oder remote)
	•	systemd Service
	•	nginx Reverse Proxy
	•	DEV / PROD Modus
	•	automatische Host- & CSRF-Konfiguration
	•	Update-Skript mit Fehler-Logging

Optimiert für:
	•	Proxmox LXC
	•	normale Debian/Ubuntu Server
	•	Reverse-Proxy-Setups (HTTPS extern)

⸻

✨ Features
	•	Debian 12 & Ubuntu 22.04+ kompatibel
	•	eigener Linux-App-User
	•	separater PostgreSQL-User
	•	.env-basierte Konfiguration
	•	automatisches ALLOWED_HOSTS
	•	automatisches CSRF_TRUSTED_ORIGINS
	•	nginx server_name automatisch gesetzt
	•	reproduzierbares Setup
	•	Update-Skript mit Logs

⸻

🧠 Architektur (wichtig!)

Bereich	Zweck
Linux-User (APPUSER)	Startet Django, besitzt venv
PostgreSQL-User (DBUSER)	DB-Zugriff für Django
/srv/<projekt>	Projekt + venv
.env	Einziger Ort für Secrets

👉 Linux-User ≠ DB-User (Absicht!)

⸻

📦 Installation

chmod +x install.sh
sudo ./install.sh

Das Skript fragt u. a. ab:
	•	Projektname → /srv/<projekt>
	•	DEV oder PROD
	•	ALLOWED_HOSTS
	•	Linux-App-User
	•	PostgreSQL DB-Name
	•	PostgreSQL DB-User
	•	PostgreSQL Passwort
	•	lokale oder Remote-DB
	•	Django SECRET_KEY

⸻

🔧 DEV / PROD Modus

DEV
	•	DEBUG=True
	•	geeignet für LAN / Tests

PROD
	•	DEBUG=False
	•	vorbereitet für Reverse Proxy (HTTPS)
	•	SECURE_PROXY_SSL_HEADER aktiv

Modus wird in .env gespeichert:

MODE=dev | prod


⸻

🌍 Hosts & CSRF (automatisch!)

Du gibst nur ALLOWED_HOSTS an:

ALLOWED_HOSTS=192.168.1.10,localhost,gps.example.com

Das Skript erzeugt automatisch:

CSRF_TRUSTED_ORIGINS=https://gps.example.com

✔ IPs & localhost werden ignoriert
✔ nur DNS-Namen → https://…
✔ perfekt für Reverse-Proxy-Setups

⸻

🌐 nginx

server_name wird automatisch aus ALLOWED_HOSTS gebaut:

server_name gps.example.com 192.168.1.10 localhost;

nginx kümmert sich nicht um CSRF – das ist rein Django.

⸻

🗄️ Datenbank

Lokal
	•	PostgreSQL wird installiert
	•	DB + User werden angelegt

Remote
	•	DB-Zugangsdaten werden abgefragt
	•	kein lokaler PostgreSQL nötig

Alle DB-Zugangsdaten landen nur in:

/srv/<projekt>/.env

Rechte:

chmod 600 .env
chown <appuser>:<appuser> .env


⸻

🔐 Security-Hinweise

✔ keine Secrets im Code
✔ keine Secrets im systemd-Service
✔ .env nur für root + App-User
❌ .env darf niemals ins Git-Repo

Empfohlene .gitignore:

.env
.venv/
__pycache__/


⸻

▶️ Superuser anlegen

sudo -u <appuser> /srv/<projekt>/.venv/bin/python /srv/<projekt>/manage.py createsuperuser

Admin:

http://SERVER-IP/admin

(Extern über deinen Reverse Proxy)

⸻

🔄 Update-Skript (mit Logging)

Bei der Installation wird automatisch erstellt:

/usr/local/sbin/<projekt>_update.sh

Update ausführen

sudo /usr/local/sbin/<projekt>_update.sh

Was macht es?
	1.	git pull (wenn Repo vorhanden)
	2.	pip install -r requirements.txt (optional)
	3.	python manage.py migrate
	4.	systemctl restart <projekt>
	5.	Logging in /var/log/<projekt>/

Logs ansehen

ls -lah /var/log/<projekt>/
tail -n 200 /var/log/<projekt>/update-*.log


⸻

🔑 GitHub SSH Setup (für private Repos)

SSH-Key erzeugen (als App-User!)

su - <appuser>
ssh-keygen -t ed25519 -f ~/.ssh/github_ed25519 -C "github-$(hostname)"

SSH Config

nano ~/.ssh/config

Host github.com
  HostName github.com
  User git
  IdentityFile ~/.ssh/github_ed25519
  IdentitiesOnly yes

Test

ssh -T git@github.com

Repo klonen

git clone git@github.com:USER/REPO.git


⸻

⚠️ Proxmox LXC Hinweis

Container braucht:

nesting=1
keyctl=1


⸻

❌ Was das Skript bewusst NICHT macht
	•	kein HTTPS (macht dein Reverse Proxy)
	•	kein Firewall-Setup
	•	kein Backup-System
	•	kein Auto-Scaling

➡️ Ziel: klarer, stabiler Unterbau, kein Hosting-Framework.

⸻

🎯 Gedacht für
	•	interne Tools
	•	Projekt- & Behörden-Systeme
	•	LXC / VM / Bare-Metal
	•	reproduzierbare Server-Setups
