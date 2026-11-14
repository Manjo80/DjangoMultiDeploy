# Sicherheitsprüfung DjangoMultiDeploy — Stand 2026-11-14

Geprüft: `Installv2.sh`, `fix_security_headers.sh`, `patch_update_script.sh`,
`security_test.py` und das Manager-Panel (`manager/`). Befunde stammen aus
Code-Lektüre; die erzeugten nginx-Konfigurationen wurden mit nginx 1.24 und
openssl real getestet, das Panel mit seiner Testsuite (61 Tests, Django 4.2 und 5.2).

## Behoben

### Manager-Panel
| Befund | Datei |
|---|---|
| Sicherheits-Scan führte Projektcode (`manage.py check`, venv-Binaries) als **root** aus — jeder Viewer konnte ihn auslösen | `control/utils/pip_utils.py` |
| Jeder angemeldete Benutzer konnte den privaten SSH-Schlüssel jedes Projekts abrufen | `control/views/install.py` |
| Zweiter Faktor (TOTP) ohne Fehlversuchs-Limit; unverifizierte Sitzung konnte 2FA abschalten | `control/views/auth.py` |
| Client-IP über `CF-Connecting-IP`/`X-Forwarded-For` fälschbar (IP-Whitelist, Login-Drossel) | `control/middleware.py` |
| Bei Bindung an die LAN-IP galt jeder Besucher als „der Server selbst" | `djmanager/settings.py` |
| `ALLOWED_HOSTS` wurde ungeprüft in die nginx-Config geschrieben (Config-Injection) | `control/views/projects.py` |
| Scans gegen beliebige Ziele; nuclei/ZAP auch für Viewer | `control/views/projects.py` |
| Operator konnte den Manager-Dienst stoppen; Install-Logs für alle lesbar | `admin_views.py`, `install.py` |
| Zugriffs-Log-Daten (URLs von Besuchern) ungefiltert per `innerHTML` | `project_detail.html` |
| Klon-ZIP enthielt private Uploads/Backups; Log-Zeilenzahl unbegrenzt | `control/views/projects.py` |
| Django 4.2 ohne Sicherheitsupdates (EOL April 2026) → 5.2 LTS | `requirements.txt` |

### Installer und erzeugte Server-Konfiguration
| Befund | Wirkung |
|---|---|
| sudoers erlaubte `journalctl`/`systemctl status` als root → Root-Shell über den Pager | nur noch `restart`/`reload`; Logs über Gruppe `systemd-journal` |
| Manager-Gunicorn (root) lauschte per HTTP auf der LAN-IP | neue Installationen: nur `127.0.0.1` |
| Admin-Passwörter auf der Kommandozeile (`ps`) und Shell-Injection über `'` | Übergabe per stdin/Umgebung, Benutzername validiert |
| `add_header` in Login-/Static-Location verwarf **alle** Security-Header dort | entfernt |
| TLS: CBC- und RSA-Suites ohne Forward Secrecy, Session-Tickets an | nur ECDHE-AEAD, Tickets aus |
| `Server: nginx/1.x.y` | `server_tokens off` |
| `/media/`: HTML/SVG/JS liefen im Origin der App; `.env`, Backups, Dumps abrufbar | Sandbox-Download, 404 für Punkt-Dateien/Backups/Dumps |
| `realip.conf` + `cloudflare_realip.conf` = doppeltes `real_ip_header` → nginx-Config ungültig, Reloads schlugen still fehl | nur noch eine Datei |
| nginx-Default-Site („Welcome to nginx") wurde nie entfernt | behoben |
| GitHub über Port 443 mit `StrictHostKeyChecking=no` | gepinnter Hostkey |
| Backups: DB-Dumps/`.env`-Kopien weltlesbar angelegt und nie gelöscht | `umask 077`, Rotation für alle Arten |
| systemd-Unit ohne Härtung, ohne `ExecReload` | `NoNewPrivileges` u. a. |

### Bestehende Server
`harden_server.sh` (neu) bringt vorhandene Installationen auf diesen Stand und
wird von `fix_security_headers.sh` aufgerufen — also bei jedem Manager-Update.
Direkt ausführen: `sudo bash harden_server.sh && nginx -t && systemctl reload nginx`.
Nicht automatisch umgestellt wird die Bindung des Manager-Gunicorn (siehe unten).

## Offen (bewusst nicht automatisch geändert)

| # | Befund | Empfehlung |
|---|---|---|
| 1 | **Update/Reset führt `git` als root im Repo des App-Benutzers aus** (`Installv2.sh` Update-Skript, `utils/deployment.py`). Git-Hooks oder `.git/config` des App-Benutzers laufen dann als root. | git als App-Benutzer ausführen (`runuser -u`), Deploy-Key dafür lesbar machen, `safe.directory` entfernen. Größerer Umbau — braucht Test auf einem echten Server. |
| 2 | **ZIP-Update entpackt als root** ins App-Verzeichnis und folgt Symlinks (`utils/deployment.py:425`). | als App-Benutzer entpacken oder `O_NOFOLLOW`. |
| 3 | **Bestehende Manager-Installationen** lauschen weiter auf der LAN-IP:8888 (nur durch ufw geschützt). | In `/etc/systemd/system/djmanager.service` `--bind 127.0.0.1:8888` setzen, in der nginx-Site `proxy_pass http://127.0.0.1:8888`, dann `systemctl daemon-reload && systemctl restart djmanager && systemctl reload nginx`. |
| 4 | root schreibt in Verzeichnisse des App-Benutzers (Backup-Ordner, `/var/log/<projekt>`, `.env`) → Symlink-Angriffe. | Backup-Ordner `root:root`, logrotate mit `su`. |
| 5 | Vorhersagbare Pfade in `/tmp` (`djmanager_installs`, `dmd_uploads`, Klon-ZIP), ZAP-API-Key fest im Code, ZAP läuft als root. | nach `/var/lib/djmanager` (0700) verlegen, `PrivateTmp=yes`, zufälliger Key. |
| 6 | fail2ban startet auf frischen Systemen vermutlich nicht (`logpath` ohne Treffer, kein `auth.log` auf Debian 12). | sshd-Jail mit `backend = systemd`, nginx-Jails erst nach Site-Erstellung. |
| 7 | Entwicklungsmodus (`DEBUG=True`) ist Standardauswahl und wird veröffentlicht. | Standard auf Produktion. |
| 8 | SSH: App-Benutzer mit Passwort, keine sshd-Härtung, kein `unattended-upgrades`. | `PasswordAuthentication no`, `ufw limit 22/tcp`, automatische Sicherheitsupdates. |
| 9 | Doppelte Header (nginx **und** Django setzen X-Frame-Options usw.); CSP erlaubt ganz `cdn.jsdelivr.net`. | Eine Quelle je Header; Bootstrap lokal ausliefern. |
| 10 | State-Datei wird per `source` gelesen, Werte nicht escaped; `.env`-Werte nur teilweise. | `printf '%q'`. |
| 11 | `security_test.py`: TLS-1.0/1.1-Test und Brute-Force-Test liefern falsche Erfolge; Header nur auf `/` geprüft. | Tests auf `/login/`, `/static/`, `/media/` ausweiten. |
