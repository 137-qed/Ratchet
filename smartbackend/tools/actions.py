"""
Ratchet — Self-Healing Action Tools
Provides actions the agent can take to remediate issues:
restart services, kill processes, clear caches, and (optionally) reboot.
"""

import subprocess
import os
import time
import logging

logger = logging.getLogger('ratchet.actions')

# Action history for audit trail
_action_history = []


def _record_action(action_type, details, success, message):
    """Record an action in the audit history."""
    entry = {
        'timestamp': time.time(),
        'action': action_type,
        'details': details,
        'success': success,
        'message': message,
    }
    _action_history.append(entry)
    if len(_action_history) > 100:
        _action_history.pop(0)
    logger.info(f"Action: {action_type} | Success: {success} | {message}")
    return entry


def get_action_history():
    """Return the recorded action history."""
    return list(_action_history)


def restart_service(service_name):
    """
    Restart a systemd service.
    Returns {success: bool, message: str}
    """
    if not service_name or not service_name.replace('-', '').replace('_', '').replace('.', '').isalnum():
        return _record_action('restart_service', service_name, False,
                              f"Invalid service name: {service_name}")

    try:
        # Try systemctl
        result = subprocess.run(
            ['systemctl', 'restart', service_name],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            return _record_action('restart_service', service_name, True,
                                  f"Successfully restarted service: {service_name}")
        else:
            # Try service command as fallback
            result2 = subprocess.run(
                ['service', service_name, 'restart'],
                capture_output=True, text=True, timeout=30
            )
            if result2.returncode == 0:
                return _record_action('restart_service', service_name, True,
                                      f"Successfully restarted service (via service cmd): {service_name}")

            return _record_action('restart_service', service_name, False,
                                  f"Failed to restart {service_name}: {result.stderr.strip()}")

    except subprocess.TimeoutExpired:
        return _record_action('restart_service', service_name, False,
                              f"Timeout restarting service: {service_name}")
    except FileNotFoundError:
        return _record_action('restart_service', service_name, False,
                              "systemctl/service command not found")


def kill_process(pid, signal=9):
    """
    Kill a process by PID.
    Returns {success: bool, message: str}
    """
    try:
        pid = int(pid)
    except (ValueError, TypeError):
        return _record_action('kill_process', str(pid), False,
                              f"Invalid PID: {pid}")

    # Safety: don't kill PID 1 or our own process
    if pid <= 1:
        return _record_action('kill_process', str(pid), False,
                              f"Cannot kill PID {pid} (system process)")
    if pid == os.getpid():
        return _record_action('kill_process', str(pid), False,
                              "Cannot kill own process")

    try:
        os.kill(pid, signal)
        return _record_action('kill_process', str(pid), True,
                              f"Sent signal {signal} to PID {pid}")
    except ProcessLookupError:
        return _record_action('kill_process', str(pid), False,
                              f"Process {pid} not found")
    except PermissionError:
        return _record_action('kill_process', str(pid), False,
                              f"Permission denied killing PID {pid}")


def clear_cache():
    """
    Clear system caches (Linux page cache, dentries, inodes).
    Requires appropriate permissions.
    Returns {success: bool, message: str}
    """
    actions_taken = []

    # Clear /tmp files older than 1 day
    try:
        result = subprocess.run(
            ['find', '/tmp', '-type', 'f', '-mtime', '+1', '-delete'],
            capture_output=True, text=True, timeout=30
        )
        if result.returncode == 0:
            actions_taken.append("Cleaned old /tmp files")
    except (subprocess.TimeoutExpired, FileNotFoundError):
        pass

    # Try to drop page cache (needs root)
    try:
        subprocess.run(['sync'], capture_output=True, timeout=10)
        result = subprocess.run(
            ['sh', '-c', 'echo 3 > /proc/sys/vm/drop_caches'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            actions_taken.append("Dropped page cache")
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError):
        pass

    if actions_taken:
        return _record_action('clear_cache', ', '.join(actions_taken), True,
                              f"Cache cleared: {', '.join(actions_taken)}")
    else:
        return _record_action('clear_cache', 'none', False,
                              "No cache actions could be performed (may need root)")


def reboot_device(confirm=False):
    """
    Reboot the device. Requires explicit confirmation flag.
    Returns {success: bool, message: str}
    """
    if not confirm:
        return _record_action('reboot_device', 'not confirmed', False,
                              "Reboot requires explicit confirmation (confirm=True)")

    try:
        result = subprocess.run(
            ['shutdown', '-r', '+1', 'Ratchet agent initiated reboot'],
            capture_output=True, text=True, timeout=10
        )
        if result.returncode == 0:
            return _record_action('reboot_device', 'scheduled', True,
                                  "Reboot scheduled in 1 minute")
        else:
            return _record_action('reboot_device', 'failed', False,
                                  f"Reboot failed: {result.stderr.strip()}")
    except (subprocess.TimeoutExpired, FileNotFoundError, PermissionError) as e:
        return _record_action('reboot_device', 'error', False,
                              f"Reboot error: {str(e)}")


if __name__ == '__main__':
    import json
    # Demo: show action history (no actions taken)
    print("Action tools loaded. Available: restart_service, kill_process, clear_cache, reboot_device")
    print(f"Action history: {json.dumps(get_action_history(), indent=2)}")
