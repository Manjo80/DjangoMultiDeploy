Hier ist die **saubere, funktionierende SSH-Key-Anleitung** für GitHub – ohne Umwege, ohne Theorie, genau so, wie du sie auf deinem Debian/LXC-Server brauchst, um dein Skript zu **clonen, pullen und später weiterzuverwenden**.

Ich gehe davon aus:
✔ du bist auf dem Server per SSH eingeloggt
✔ du willst **ohne Passwort** auf **private Repos** zugreifen
✔ du willst es stabil für Folgeprojekte

---

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
