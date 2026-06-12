"""
Notification dispatch — e-mail (SMTP) and/or generic JSON webhook.

A single entry point, ``send_notification()``, reads the NotificationSettings
singleton and fans out to every enabled channel. All delivery failures are
swallowed and logged: an alert that cannot be delivered must never break the
action that triggered it (a backup, a scan, a status check).
"""
import json
import logging
import smtplib
import ssl
import urllib.request
from email.message import EmailMessage

logger = logging.getLogger('djmanager.notify')

# Event type constants — also used to gate per-event toggles.
EVENT_BACKUP_FAILURE = 'backup_failure'
EVENT_SERVICE_DOWN   = 'service_down'
EVENT_VULNERABILITY  = 'vulnerability'

_EVENT_TOGGLE = {
    EVENT_BACKUP_FAILURE: 'notify_backup_failure',
    EVENT_SERVICE_DOWN:   'notify_service_down',
    EVENT_VULNERABILITY:  'notify_vulnerabilities',
}


def _settings():
    from ..models import NotificationSettings
    return NotificationSettings.get()


def send_notification(subject, body, event_type=None):
    """
    Dispatch an alert to all enabled channels.

    If event_type is given, the matching per-event toggle must also be on.
    Returns a dict {'email': bool|None, 'webhook': bool|None} where None means
    the channel was disabled and bool is the delivery outcome.
    """
    result = {'email': None, 'webhook': None}
    try:
        cfg = _settings()
    except Exception:
        logger.exception('notify: could not load settings')
        return result

    if not cfg.enabled:
        return result
    if event_type is not None:
        toggle = _EVENT_TOGGLE.get(event_type)
        if toggle and not getattr(cfg, toggle, True):
            return result

    if cfg.email_enabled and cfg.smtp_host and cfg.recipients():
        result['email'] = _send_email(cfg, subject, body)
    if cfg.webhook_enabled and cfg.webhook_url:
        result['webhook'] = _send_webhook(cfg, subject, body)
    return result


def _send_email(cfg, subject, body):
    try:
        msg = EmailMessage()
        msg['Subject'] = f'[DjangoMultiDeploy] {subject}'
        msg['From'] = cfg.smtp_from or (cfg.smtp_user or 'djmanager@localhost')
        msg['To'] = ', '.join(cfg.recipients())
        msg.set_content(body)

        port = cfg.smtp_port or 587
        if port == 465:
            ctx = ssl.create_default_context()
            with smtplib.SMTP_SSL(cfg.smtp_host, port, timeout=15, context=ctx) as s:
                _smtp_login_send(s, cfg, msg)
        else:
            with smtplib.SMTP(cfg.smtp_host, port, timeout=15) as s:
                if cfg.smtp_use_tls:
                    s.starttls(context=ssl.create_default_context())
                _smtp_login_send(s, cfg, msg)
        return True
    except Exception:
        logger.exception('notify: e-mail delivery failed')
        return False


def _smtp_login_send(s, cfg, msg):
    if cfg.smtp_user:
        s.login(cfg.smtp_user, cfg.smtp_password)
    s.send_message(msg)


def _send_webhook(cfg, subject, body):
    # "text" is what Slack / Mattermost / Discord(content via compat) render;
    # the extra fields make the payload useful for generic receivers too.
    payload = json.dumps({
        'text': f'*{subject}*\n{body}',
        'subject': subject,
        'body': body,
    }).encode()
    try:
        req = urllib.request.Request(
            cfg.webhook_url, data=payload,
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(req, timeout=15) as resp:
            return 200 <= resp.status < 300
    except Exception:
        logger.exception('notify: webhook delivery failed')
        return False
