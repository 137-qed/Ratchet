"""Read Linux system counters through an authorized SSH connection."""

from pathlib import Path
import re
import socket
import subprocess
from threading import Event, Lock, Thread, Timer
import time

import paramiko

from device_telemetry import OFFLINE_AFTER_SECONDS, _normalize_sample


USERNAME = re.compile(r'[A-Za-z0-9_][A-Za-z0-9_.-]{0,63}')
HOSTNAME = re.compile(r'(?=.{1,253}\Z)[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?')
POLL_SECONDS = 10
MAX_CONNECTIONS = 16
READ_COUNTERS = r"""
export LC_ALL=C PATH=/usr/bin:/bin
set -e
test -r /proc/stat && test -r /proc/meminfo && test -r /proc/uptime
hostname
uname -sr
head -n 1 /proc/stat
sleep 1
head -n 1 /proc/stat
awk '/^MemTotal:/ {total=$2} /^MemAvailable:/ {available=$2} END {print total, available}' /proc/meminfo
df -P / | awk 'NR == 2 {sub(/%/, "", $5); print $5}'
cut -d ' ' -f 1 /proc/uptime
"""


def _read_with_key(address, username, port, trusted_host):
    alias_options = []
    if trusted_host:
        alias = trusted_host if port == 22 else f'[{trusted_host}]:{port}'
        alias_options = ['-o', f'HostKeyAlias={alias}']
    try:
        result = subprocess.run([
            'ssh', '-F', '/dev/null', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=4',
            '-o', 'StrictHostKeyChecking=yes', '-o', 'UpdateHostKeys=no',
            '-o', 'ClearAllForwardings=yes', '-o', 'PreferredAuthentications=publickey',
            *alias_options, '-l', username, '-p', str(port), '--', address, READ_COUNTERS,
        ], stdin=subprocess.DEVNULL, capture_output=True, text=True, errors='replace', timeout=10)
    except FileNotFoundError as error:
        raise ValueError('Install the OpenSSH client on the Ratchet host.') from error
    except subprocess.TimeoutExpired as error:
        raise ValueError('SSH timed out. Check that SSH is reachable on the selected device.') from error

    if result.returncode:
        message = result.stderr.lower()
        if 'host key verification failed' in message or 'remote host identification has changed' in message:
            raise ValueError('Could not verify the SSH host key. If it is saved under a hostname, enter that name.')
        if 'permission denied' in message:
            raise ValueError('SSH rejected the key login. Enter the device’s SSH password if it accepts passwords.')
        if result.returncode == 255:
            raise ValueError('Could not reach the SSH service. Check the device and port.')
        raise ValueError('SSH connected, but the device could not provide Linux system counters.')
    return result.stdout


def _connect_with_password(address, username, port, password, trusted_host):
    client = paramiko.SSHClient()
    sock = None
    connected = False
    try:
        client.load_system_host_keys()
        for path in ('/etc/ssh/ssh_known_hosts', '/etc/ssh/ssh_known_hosts2'):
            if Path(path).is_file():
                client.load_system_host_keys(path)
        sock = socket.create_connection((address, port), timeout=4)
        client.connect(
            hostname=trusted_host or address, port=port, username=username, password=password,
            sock=sock, allow_agent=False, look_for_keys=False, timeout=5, banner_timeout=5, auth_timeout=5,
            transport_factory=paramiko.ServiceRequestingTransport,
        )
        connected = True
        return client
    except paramiko.BadHostKeyException as error:
        raise ValueError('The SSH host key differs from the saved key. Verify the device’s identity before connecting.') from error
    except paramiko.AuthenticationException as error:
        raise ValueError('SSH rejected the password login. Check the username and password on the device.') from error
    except paramiko.SSHException as error:
        if 'not found in known_hosts' in str(error):
            raise ValueError('Could not verify the SSH host key. If it is saved under a hostname, enter that name.') from error
        raise ValueError('Could not establish the SSH session. Check that SSH is enabled on the device.') from error
    except (OSError, EOFError) as error:
        raise ValueError('Could not reach the SSH service. Check the device and port.') from error
    finally:
        if not connected:
            client.close()
            if sock is not None:
                sock.close()


def _read_password_session(client):
    transport = client.get_transport()
    if transport is None or not transport.is_active():
        raise ValueError('The SSH session ended. Reconnect to resume live metrics.')
    # Bound the command even if the server opens a channel and never acknowledges the exec request.
    deadline = Timer(8, client.close)
    deadline.daemon = True
    deadline.start()
    try:
        with transport.open_session(timeout=5) as channel:
            channel.settimeout(5)
            channel.exec_command(READ_COUNTERS)
            with channel.makefile('rb') as stdout:
                output = stdout.read(8193)
            if len(output) > 8192 or channel.recv_exit_status() != 0:
                raise ValueError('SSH connected, but the device could not provide Linux system counters.')
            return output.decode('utf-8', errors='replace')
    except (paramiko.SSHException, OSError, EOFError) as error:
        raise ValueError('The SSH connection stopped responding while reading metrics.') from error
    finally:
        deadline.cancel()


def _parse_metrics(output, address):
    try:
        hostname, os_name, first, second, memory, disk, uptime = output.splitlines()
        before, after = (list(map(int, line.split()[1:9])) for line in (first, second))
        elapsed = sum(after) - sum(before)
        idle = after[3] - before[3] + after[4] - before[4]
        total, available = map(int, memory.split())
        return _normalize_sample({
            'hostname': hostname, 'os': os_name,
            'cpu_percent': round(100 * (elapsed - idle) / elapsed, 1) if elapsed > 0 else 0,
            'memory_percent': round(100 * (total - available) / total, 1),
            'disk_percent': int(disk), 'uptime_seconds': int(float(uptime)),
        }, f'ssh-{address}')
    except (ValueError, IndexError, ZeroDivisionError, OverflowError) as error:
        raise ValueError('SSH connected, but Linux system counters could not be read.') from error


class SSHMetrics:
    def __init__(self):
        self._lock = Lock()
        self._connections = {}

    def connect(self, address, username, port, *, password=None, trusted_host=None):
        if not isinstance(username, str) or not USERNAME.fullmatch(username):
            raise ValueError('Enter a valid SSH username.')
        if type(port) is not int or not 1 <= port <= 65535:
            raise ValueError('Enter an SSH port from 1 to 65535.')
        if password is not None and (not isinstance(password, str) or len(password) > 1024):
            raise ValueError('Enter an SSH password of at most 1024 characters.')
        if trusted_host is not None and (not isinstance(trusted_host, str) or not HOSTNAME.fullmatch(trusted_host)):
            raise ValueError('Enter a valid saved SSH hostname.')

        stop = Event()
        with self._lock:
            if address in self._connections:
                raise ValueError('An SSH connection is already active or starting for this device.')
            if len(self._connections) >= MAX_CONNECTIONS:
                raise ValueError('Disconnect another SSH device before starting a new connection.')
            self._connections[address] = (stop, None)

        client = None
        try:
            if password:
                client = _connect_with_password(address, username, port, password, trusted_host)
            record = self._sample(address, username, port, trusted_host, client)
        except (ValueError, OSError):
            if client:
                client.close()
            self._remove(address, stop)
            raise
        with self._lock:
            if stop.is_set():
                if client:
                    client.close()
                raise ValueError('The SSH connection was canceled.')
            self._connections[address] = (stop, record)
        Thread(target=self._poll, args=(address, username, port, trusted_host, client, stop),
               name='ssh-metrics', daemon=True).start()
        return record['id']

    def disconnect(self, address):
        with self._lock:
            if connection := self._connections.pop(address, None):
                connection[0].set()
                return True
            return False

    def _remove(self, address, stop):
        with self._lock:
            if not stop.is_set():
                self._connections.pop(address, None)

    def _poll(self, address, username, port, trusted_host, client, stop):
        try:
            while not stop.wait(POLL_SECONDS):
                try:
                    record = self._sample(address, username, port, trusted_host, client)
                except (ValueError, OSError):
                    transport = client.get_transport() if client else None
                    if client and (transport is None or not transport.is_active()):
                        return
                    continue
                with self._lock:
                    if not stop.is_set():
                        self._connections[address] = (stop, record)
        finally:
            if client:
                client.close()
            self._remove(address, stop)

    @staticmethod
    def _sample(address, username, port, trusted_host, client):
        output = _read_password_session(client) if client else _read_with_key(address, username, port, trusted_host)
        metrics = _parse_metrics(output, address)
        return {'id': f'ssh:{address}', 'address': address,
                'last_seen': time.time(), 'metrics': metrics, 'source': 'ssh'}

    def devices(self):
        expiry = time.time() - OFFLINE_AFTER_SECONDS
        with self._lock:
            return [dict(record, online=record['last_seen'] >= expiry)
                    for _, record in self._connections.values() if record]
