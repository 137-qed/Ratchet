"""
Ratchet — Alert & Notification Tools
Supports Telegram, email, and local file-based reporting.
All channels are optional and degrade gracefully if not configured.
"""

import json
import time
import os
import logging
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

logger = logging.getLogger('ratchet.alerts')

# In-memory alert history
_alert_history = []
REPORTS_DIR = os.environ.get('RATCHET_REPORTS_DIR', os.path.join(os.path.dirname(os.path.dirname(__file__)), 'reports'))


def _record_alert(channel, success, message, details=None):
    """Record alert in history."""
    entry = {
        'timestamp': time.time(),
        'channel': channel,
        'success': success,
        'message': message,
        'details': details,
    }
    _alert_history.append(entry)
    if len(_alert_history) > 200:
        _alert_history.pop(0)
    return entry


def get_alert_history():
    """Return the recorded alert history."""
    return list(_alert_history)


def send_telegram(message, config=None):
    """
    Send a message via Telegram Bot API.
    config should have: bot_token, chat_id
    """
    if not config or not config.get('enabled'):
        logger.debug("Telegram not configured, skipping")
        return _record_alert('telegram', False, 'Telegram not configured')

    bot_token = config.get('bot_token', '')
    chat_id = config.get('chat_id', '')

    if not bot_token or not chat_id:
        return _record_alert('telegram', False, 'Missing bot_token or chat_id')

    try:
        import requests
        url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
        payload = {
            'chat_id': chat_id,
            'text': f"🔧 Ratchet Agent\n\n{message}",
            'parse_mode': 'HTML',
        }
        resp = requests.post(url, json=payload, timeout=10)
        if resp.status_code == 200:
            return _record_alert('telegram', True, 'Message sent', message[:100])
        else:
            return _record_alert('telegram', False,
                                 f'HTTP {resp.status_code}: {resp.text[:100]}')
    except Exception as e:
        return _record_alert('telegram', False, f'Error: {str(e)}')


def send_email(subject, body, config=None):
    """
    Send an email via SMTP.
    config should have: smtp_host, smtp_port, username, password, to_address
    """
    if not config or not config.get('enabled'):
        logger.debug("Email not configured, skipping")
        return _record_alert('email', False, 'Email not configured')

    smtp_host = config.get('smtp_host', '')
    smtp_port = config.get('smtp_port', 587)
    username = config.get('username', '')
    password = config.get('password', '')
    to_addr = config.get('to_address', '')

    if not all([smtp_host, username, password, to_addr]):
        return _record_alert('email', False, 'Missing SMTP configuration')

    try:
        msg = MIMEMultipart()
        msg['From'] = username
        msg['To'] = to_addr
        msg['Subject'] = f"[Ratchet] {subject}"

        msg.attach(MIMEText(body, 'plain'))

        with smtplib.SMTP(smtp_host, smtp_port, timeout=15) as server:
            server.starttls()
            server.login(username, password)
            server.send_message(msg)

        return _record_alert('email', True, f'Email sent: {subject}', to_addr)
    except Exception as e:
        return _record_alert('email', False, f'SMTP error: {str(e)}')


def write_report(data, report_type='anomaly'):
    """
    Write a JSON report to the reports/ directory.
    Always works — no external config needed.
    """
    os.makedirs(REPORTS_DIR, exist_ok=True)

    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    filename = f"{report_type}_{timestamp}.json"
    filepath = os.path.join(REPORTS_DIR, filename)

    report = {
        'report_type': report_type,
        'generated_at': datetime.now().isoformat(),
        'data': data,
    }

    try:
        with open(filepath, 'w') as f:
            json.dump(report, f, indent=2, default=str)

        logger.info(f"Report written: {filepath}")
        return _record_alert('report', True, f'Report saved: {filename}', filepath)
    except Exception as e:
        return _record_alert('report', False, f'Write error: {str(e)}')


def send_all_alerts(message, subject=None, report_data=None, config=None):
    """
    Send alerts through all configured channels.
    Graceful — skips any channel that isn't configured.
    """
    results = []

    # Always write a local report
    if report_data:
        results.append(write_report(report_data))

    alert_config = config or {}

    # Telegram
    tg_config = alert_config.get('telegram', {})
    if tg_config.get('enabled'):
        results.append(send_telegram(message, tg_config))

    # Email
    email_config = alert_config.get('email', {})
    if email_config.get('enabled'):
        results.append(send_email(subject or 'Alert', message, email_config))

    return results


if __name__ == '__main__':
    # Demo: write a test report
    test_data = {
        'anomaly': 'CPU spike detected',
        'cpu_percent': 95.3,
        'action_taken': 'Monitoring'
    }
    result = write_report(test_data, 'test')
    print(json.dumps(result, indent=2, default=str))
