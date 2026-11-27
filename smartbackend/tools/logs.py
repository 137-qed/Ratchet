"""
Ratchet — System Log Reader Tool
Reads recent system logs from journalctl, dmesg, and syslog.
"""

import subprocess
import os
import time


def _run_cmd(cmd, timeout=10):
    """Run a shell command and return its output."""
    try:
        result = subprocess.run(
            cmd, capture_output=True, text=True, timeout=timeout, shell=True
        )
        return result.stdout.strip() if result.returncode == 0 else None
    except (subprocess.TimeoutExpired, FileNotFoundError, OSError):
        return None


def read_journalctl(since_minutes=5, lines=50, priority=None):
    """
    Read recent entries from journalctl.
    Priority levels: 0=emerg, 1=alert, 2=crit, 3=err, 4=warn, 5=notice, 6=info, 7=debug
    """
    cmd = f'journalctl --since "{since_minutes} minutes ago" --no-pager -n {lines}'
    if priority is not None:
        cmd += f' -p {priority}'

    output = _run_cmd(cmd)
    if output:
        entries = []
        for line in output.split('\n'):
            line = line.strip()
            if line:
                entries.append(line)
        return entries
    return []


def read_dmesg(lines=50):
    """Read recent kernel messages from dmesg."""
    output = _run_cmd(f'dmesg --time-format reltime 2>/dev/null || dmesg | tail -n {lines}')
    if output:
        entries = output.split('\n')[-lines:]
        return [e.strip() for e in entries if e.strip()]
    return []


def read_syslog(lines=50):
    """Read recent entries from /var/log/syslog."""
    syslog_paths = ['/var/log/syslog', '/var/log/messages']

    for path in syslog_paths:
        if os.path.isfile(path) and os.access(path, os.R_OK):
            output = _run_cmd(f'tail -n {lines} {path}')
            if output:
                return [e.strip() for e in output.split('\n') if e.strip()]
    return []


def read_app_logs(log_dir='/var/log', pattern='*.log', lines=30):
    """Read recent entries from application log files."""
    app_logs = {}
    try:
        if os.path.isdir(log_dir):
            for fname in os.listdir(log_dir):
                if fname.endswith('.log') and os.access(os.path.join(log_dir, fname), os.R_OK):
                    output = _run_cmd(f'tail -n {lines} {os.path.join(log_dir, fname)}')
                    if output:
                        app_logs[fname] = [e.strip() for e in output.split('\n') if e.strip()]
    except (PermissionError, OSError):
        pass
    return app_logs


def search_errors(since_minutes=10, lines=30):
    """Search for error-level messages across log sources."""
    errors = []

    # journalctl errors (priority 0-3: emerg, alert, crit, err)
    journal_errors = read_journalctl(since_minutes=since_minutes, lines=lines, priority=3)
    for entry in journal_errors:
        errors.append({'source': 'journalctl', 'message': entry})

    # dmesg errors
    dmesg = _run_cmd(f'dmesg --level=err,crit,alert,emerg 2>/dev/null | tail -n {lines}')
    if dmesg:
        for line in dmesg.split('\n'):
            if line.strip():
                errors.append({'source': 'dmesg', 'message': line.strip()})

    return errors


def read_recent_logs(lines=50, since_minutes=5):
    """
    Main entry point — reads recent logs from all available sources.
    Returns dict with journal, dmesg, syslog, errors.
    """
    return {
        'timestamp': time.time(),
        'journal': read_journalctl(since_minutes=since_minutes, lines=lines),
        'dmesg': read_dmesg(lines=lines),
        'syslog': read_syslog(lines=lines),
        'errors': search_errors(since_minutes=since_minutes * 2, lines=lines),
    }


if __name__ == '__main__':
    import json
    logs = read_recent_logs()
    print(json.dumps(logs, indent=2, default=str))
