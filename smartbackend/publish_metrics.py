"""Send local, read-only health readings to a Ratchet telemetry receiver."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import sys
from tempfile import NamedTemporaryFile
import time
from urllib.parse import urlsplit

import psutil
import requests

from device_discovery import discover_receivers
from tools.metrics import get_cpu_metrics, get_memory_metrics, get_system_info, get_temperature


def collect_sample():
    system = get_system_info()
    temperature = get_temperature()
    try:
        connections = len(psutil.net_connections(kind="inet"))
    except (psutil.AccessDenied, OSError):
        connections = None
    return {
        "hostname": system["hostname"] or "Edge Device",
        "os": system["os"],
        "uptime_seconds": max(0, int(system["uptime_seconds"])),
        "cpu_percent": get_cpu_metrics()["percent"],
        "memory_percent": get_memory_metrics()["percent"],
        "disk_percent": psutil.disk_usage(Path.home().anchor or os.sep).percent,
        "temperature_celsius": temperature["celsius"] if temperature["sensors"] else None,
        "network_connections": connections,
        "process_count": len(psutil.pids()),
    }


def pairing_identity(receiver_url, requested_id):
    directory = Path(os.environ.get("RATCHET_PUBLISHER_STATE_DIR", Path.home() / ".ratchet"))
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    receiver_key = hashlib.sha256(receiver_url.encode()).hexdigest()[:24]
    if requested_id and requested_id.startswith('invite-'):
        receiver_key = f'{receiver_key}-{requested_id}'
    path = directory / f"{receiver_key}.json"
    if not path.exists():
        with NamedTemporaryFile(mode="w", encoding="utf-8", dir=directory, delete=False) as temporary:
            temporary_path = Path(temporary.name)
            json.dump({"id": requested_id or f"device-{secrets.token_hex(6)}", "token": secrets.token_urlsafe(32)}, temporary)
        try:
            os.link(temporary_path, path)
        except FileExistsError:
            pass
        finally:
            temporary_path.unlink()
    with path.open(encoding="utf-8") as stored:
        identity = json.load(stored)
    if requested_id and identity["id"] != requested_id:
        raise ValueError(f"This receiver is already configured with device ID {identity['id']}")
    return identity["id"], identity["token"]


def wait_for_pairing(session, receiver_url, device_id, token, verify):
    system = get_system_info()
    details = {"hostname": system["hostname"] or device_id, "os": system["os"]}
    endpoint = f"{receiver_url}/api/v1/pairings/{device_id}"
    announced = False
    while True:
        response = session.put(endpoint, json=details, verify=verify, timeout=(3, 8), allow_redirects=False)
        if response.status_code == 200:
            return
        if response.status_code != 202:
            raise ValueError(f"Receiver rejected the connection request (HTTP {response.status_code})")
        if not announced:
            code = hashlib.sha256(token.encode()).hexdigest()[:6].upper()
            print(f"In Ratchet, open Nearby Devices and click Connect for {details['hostname']} ({code}).", flush=True)
            announced = True
        time.sleep(3)


def main():
    parser = argparse.ArgumentParser(description="Publish this device's health metrics to Ratchet.")
    parser.add_argument("--url", help="Receiver base URL, such as https://ratchet.example.net:5001")
    parser.add_argument("--discover", action="store_true", help="Find Ratchet on this LAN and request a connection")
    parser.add_argument("--pair", action="store_true", help="Request a connection at the given receiver URL")
    parser.add_argument("--invitation", type=Path, help="Invitation file from the Ratchet publisher download")
    parser.add_argument("--device-id", help="Device ID enrolled on the receiver")
    parser.add_argument("--interval", type=int, default=10, choices=range(1, 31), metavar="SECONDS")
    parser.add_argument("--ca-bundle", help="PEM CA bundle for a private receiver certificate")
    parser.add_argument("--once", action="store_true", help="Send one sample and exit")
    args = parser.parse_args()
    invitation_secret = None
    if args.invitation:
        if args.url or args.discover or args.pair or args.device_id or args.ca_bundle:
            parser.error('Use --invitation on its own to specify the pairing destination')
        try:
            invitation = json.loads(args.invitation.read_text(encoding='utf-8'))
            args.url = invitation['url']
            args.device_id = invitation['device_id']
            invitation_secret = invitation['secret']
            if not all(isinstance(value, str) and value for value in (args.url, args.device_id, invitation_secret)):
                raise ValueError('Missing invitation details')
            if invitation.get('ca_bundle'):
                args.ca_bundle = str(args.invitation.parent / invitation['ca_bundle'])
        except (OSError, ValueError, KeyError, TypeError) as error:
            parser.error(f'Could not read the publisher invitation: {error}')
        args.pair = True
    if args.device_id and not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,62}", args.device_id):
        parser.error("Use a lowercase device ID with letters, numbers, hyphens, or underscores")
    receiver_url = args.url
    pairing = args.discover or args.pair
    if args.discover:
        if receiver_url or not args.ca_bundle:
            parser.error("Use --discover with the receiver's --ca-bundle and without --url")
        try:
            discovered = discover_receivers()
        except OSError as error:
            parser.error(f"Wi-Fi discovery failed: {error}")
        if not discovered:
            parser.error("No Ratchet receiver answered on this Wi-Fi. Use --pair --url with its address if broadcasts are filtered.")
        if len(discovered) > 1:
            parser.error(f"Found multiple receivers. Select one with --pair --url: {', '.join(discovered)}")
        receiver_url = discovered[0]
    if not receiver_url:
        parser.error("Supply --url or use --discover")
    receiver_url = receiver_url.rstrip('/')
    url = urlsplit(receiver_url)
    if not url.hostname or url.username or url.password or url.query or url.fragment:
        parser.error("Supply a receiver URL without credentials, query, or fragment")
    if url.scheme != "https" and not (url.scheme == "http" and url.hostname in {"localhost", "127.0.0.1", "::1"}):
        parser.error("Use HTTPS for a remote receiver or HTTP through a loopback tunnel")
    if pairing and url.scheme != "https":
        parser.error("Pairing requires HTTPS")
    if invitation_secret:
        try:
            consent = input(f'Share this device’s health metrics with {receiver_url}? [y/N] ')
        except (EOFError, KeyboardInterrupt):
            return 1
        if consent.strip().lower() not in {'y', 'yes'}:
            return 1
    if pairing:
        try:
            device_id, token = pairing_identity(receiver_url, args.device_id)
        except (OSError, ValueError, KeyError) as error:
            parser.error(f"Could not load device pairing: {error}")
    else:
        device_id, token = args.device_id, os.environ.get("RATCHET_DEVICE_TOKEN")
        if not device_id or not token:
            parser.error("Supply --device-id and set RATCHET_DEVICE_TOKEN for an enrolled device")

    endpoint = f"{receiver_url}/api/v1/devices/{device_id}/telemetry"
    verify = args.ca_bundle or True
    with requests.Session() as session:
        if args.discover or invitation_secret:
            session.trust_env = False
        session.headers["Authorization"] = f"Bearer {token}"
        try:
            if pairing:
                if invitation_secret:
                    session.headers['X-Ratchet-Invitation'] = invitation_secret
                try:
                    wait_for_pairing(session, receiver_url, device_id, token, verify)
                except (requests.RequestException, ValueError) as error:
                    print(f"Unable to pair with Ratchet: {error}", file=sys.stderr)
                    return 1
                finally:
                    session.headers.pop('X-Ratchet-Invitation', None)
            while True:
                try:
                    response = session.put(endpoint, json=collect_sample(), timeout=(3, 8),
                                           verify=verify, allow_redirects=False)
                    if response.status_code != 200:
                        print(f"Receiver rejected the sample (HTTP {response.status_code})", file=sys.stderr)
                        if args.once or response.status_code < 500:
                            return 1
                    elif args.once:
                        print(f"Published telemetry for {device_id}")
                        return 0
                except (requests.RequestException, OSError, psutil.Error) as error:
                    print(f"Unable to publish telemetry ({type(error).__name__})", file=sys.stderr)
                    if args.once:
                        return 1
                time.sleep(args.interval)
        except KeyboardInterrupt:
            return 0


if __name__ == "__main__":
    sys.exit(main())
