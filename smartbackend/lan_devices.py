"""Find IPv4 neighbors on Ratchet's local Wi-Fi link."""

from concurrent.futures import ThreadPoolExecutor
from ipaddress import IPv4Address, IPv4Interface
import os
from pathlib import Path
import socket
from threading import Lock, Thread
import time

import psutil


class NearbyDevices:
    def __init__(self):
        self._lock = Lock()
        self._peers = {}
        self._names = {}
        self._running = False
        self._started = -float('inf')
        self._error = None
        self._resolver = ThreadPoolExecutor(max_workers=6, thread_name_prefix='lan-name')

    def snapshot(self, refresh=False):
        with self._lock:
            if not self._running and time.monotonic() - self._started >= (5 if refresh else 30):
                self._running = True
                self._started = time.monotonic()
                Thread(target=self._refresh, name='lan-discovery', daemon=True).start()
            peers = []
            for address, hardware in sorted(self._peers.items()):
                name = self._names.get((address, hardware))
                hostname = name.result() if name and name.done() else None
                peers.append({'address': address, 'hostname': hostname or address})
            return peers, self._running, self._error

    def contains(self, address):
        with self._lock:
            if address not in self._peers or time.monotonic() - self._started > 45:
                return False
        link = _local_link()
        return bool(link and IPv4Address(address) in link[1].network)

    def _refresh(self):
        peers = {}
        error = None
        try:
            link = _local_link()
            if link:
                name, interface = link
                _resolve_neighbors(interface)
                peers = _read_neighbors(name, interface)
            else:
                error = 'Connect the Ratchet host to Wi-Fi to discover nearby devices.'
        except (OSError, ValueError):
            error = 'Wi-Fi discovery is unavailable. Check the Ratchet host’s network connection.'
        finally:
            with self._lock:
                self._peers = peers
                self._error = error
                self._running = False
                self._names = {key: result for key, result in self._names.items()
                               if peers.get(key[0]) == key[1] or not result.done()}
                for peer in peers.items():
                    if peer not in self._names and len(self._names) < 128:
                        self._names[peer] = self._resolver.submit(_lookup_name, peer[0])


def _local_link():
    configured = os.environ.get('RATCHET_LAN_INTERFACE')
    stats = psutil.net_if_stats()
    for name, addresses in sorted(psutil.net_if_addrs().items()):
        if name == 'lo' or not stats.get(name) or not stats[name].isup:
            continue
        is_wireless = Path(f'/sys/class/net/{name}/wireless').is_dir()
        if (configured and name != configured) or (not configured and not is_wireless):
            continue
        for entry in addresses:
            if entry.family == socket.AF_INET and entry.netmask:
                interface = IPv4Interface(f'{entry.address}/{entry.netmask}')
                if not (interface.ip.is_loopback or interface.ip.is_link_local):
                    return name, interface
    return None


def _resolve_neighbors(interface):
    # Bound the active part of discovery to the local /24 on larger networks.
    # A zero-length datagram asks the kernel to resolve the peer's link address.
    sweep = IPv4Interface(f'{interface.ip}/{max(24, interface.network.prefixlen)}').network
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.bind((str(interface.ip), 0))
        probe.setblocking(False)
        for address in sweep.hosts():
            if address == interface.ip:
                continue
            try:
                probe.sendto(b'', (str(address), 9))
            except (BlockingIOError, ConnectionError):
                continue
    time.sleep(1)


def _read_neighbors(name, interface):
    peers = {}
    with Path('/proc/net/arp').open(encoding='ascii') as table:
        next(table, None)
        for row in table:
            address, _, flags, hardware, _, link = row.split()
            if link != name or not int(flags, 16) & 2:
                continue
            ip = IPv4Address(address)
            if ip != interface.ip and ip in interface.network:
                peers[address] = hardware
    return peers


def _lookup_name(address):
    try:
        hostname = socket.gethostbyaddr(address)[0].removesuffix('.')
        return hostname if 0 < len(hostname) <= 120 and hostname.isprintable() else None
    except OSError:
        return None
