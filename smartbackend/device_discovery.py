"""Discover opted-in Ratchet receivers over IPv4 LAN broadcast."""

from ipaddress import ip_address, ip_network
import socket
from threading import Thread
import time

import psutil


DISCOVERY_PORT = 45871
PROBE = b"RATCHET_TELEMETRY_DISCOVER_V1"
REPLY = b"RATCHET_TELEMETRY_V1 "


def start_discovery(host, http_port):
    address = ip_address(host)
    interface = next((entry for entries in psutil.net_if_addrs().values() for entry in entries
                      if entry.family == socket.AF_INET and entry.address == host), None)
    if address.version != 4 or not interface or not interface.netmask:
        raise ValueError("Discovery requires the Wi-Fi interface's IPv4 address")
    subnet = ip_network(f"{host}/{interface.netmask}", strict=False)
    listener = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        listener.bind(("", DISCOVERY_PORT))
    except OSError:
        listener.close()
        raise

    def announce():
        with listener:
            reply = REPLY + str(http_port).encode("ascii")
            while True:
                try:
                    data, peer = listener.recvfrom(64)
                    if data == PROBE and ip_address(peer[0]) in subnet:
                        listener.sendto(reply, peer)
                except OSError:
                    return

    Thread(target=announce, name="ratchet-discovery", daemon=True).start()
    return listener


def discover_receivers():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
        probe.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        probe.sendto(PROBE, ("255.255.255.255", DISCOVERY_PORT))
        deadline = time.monotonic() + 1.5
        receivers = set()
        while (remaining := deadline - time.monotonic()) > 0:
            probe.settimeout(remaining)
            try:
                data, (host, port) = probe.recvfrom(64)
            except TimeoutError:
                break
            number = data.removeprefix(REPLY)
            if port == DISCOVERY_PORT and data.startswith(REPLY) and number.isdigit() and 1 <= int(number) <= 65535:
                receivers.add(f"https://{host}:{int(number)}")
    return sorted(receivers)
