"""Receive authenticated device samples and share their latest values with Ratchet."""

import argparse
from contextlib import closing
import hashlib
import hmac
import ipaddress
from io import BytesIO
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
from zipfile import ZIP_DEFLATED, ZipFile

from flask import Flask, jsonify, request, send_file
from werkzeug.serving import WSGIRequestHandler

from device_discovery import start_discovery


DEVICE_ID = re.compile(r"[a-z0-9][a-z0-9_-]{0,62}")
DEVICE_TOKEN = re.compile(r"[A-Za-z0-9_-]{32,128}")
INVITED_DEVICE_ID = re.compile(r"invite-[a-f0-9]{24}")
PEM_CERTIFICATES = re.compile(rb"(?:\s*-----BEGIN CERTIFICATE-----\s+[a-zA-Z0-9+/=\s]+-----END CERTIFICATE-----\s*)+")
OFFLINE_AFTER_SECONDS = 45
PAIRING_TIMEOUT_SECONDS = 30
MAX_PENDING_PAIRINGS = 32
INVITATION_LIFETIME = 15 * 60
DEFAULT_DB = Path(__file__).with_name("reports") / "device-telemetry.sqlite3"


class ReceiverRequestHandler(WSGIRequestHandler):
    def log_request(self, code='-', size='-'):
        if getattr(self, 'path', '').startswith('/publisher/'):
            self.log('info', '"%s /publisher/[invitation] %s" %s %s', self.command, self.request_version, code, size)
        else:
            super().log_request(code, size)


def telemetry_db_path():
    reports = Path(os.environ.get("RATCHET_REPORTS_DIR", DEFAULT_DB.parent))
    return Path(os.environ.get("RATCHET_TELEMETRY_DB", reports / DEFAULT_DB.name))


def _is_loopback(host):
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _normalize_sample(data, device_id):
    if not isinstance(data, dict):
        raise ValueError("Telemetry must be a JSON object")

    sample = {}
    for field in ("cpu_percent", "memory_percent", "disk_percent"):
        value = data.get(field)
        if type(value) not in (float, int) or not 0 <= value <= 100:
            raise ValueError(f"{field} must be a number between 0 and 100")
        sample[field] = value

    for field, default in (("hostname", device_id), ("os", "Unknown")):
        value = data.get(field, default)
        if not isinstance(value, str) or not value.strip() or len(value) > 120 or not value.isprintable():
            raise ValueError(f"{field} must be 1 to 120 printable characters")
        sample[field] = value.strip()

    for field in ("uptime_seconds", "network_connections", "process_count"):
        value = data.get(field)
        if value is not None and (type(value) is not int or not 0 <= value <= 2**31 - 1):
            raise ValueError(f"{field} must be a nonnegative integer or null")
        sample[field] = value

    temperature = data.get("temperature_celsius")
    if temperature is not None and (type(temperature) not in (float, int) or not -273.15 <= temperature <= 500):
        raise ValueError("temperature_celsius must be a valid Celsius value or null")
    sample["temperature_celsius"] = temperature
    return sample


def _pairing_table_exists(db):
    return db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='device_pairings'").fetchone() is not None


def list_pending_pairings():
    path = telemetry_db_path()
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        if not _pairing_table_exists(db):
            return []
        rows = db.execute("""
            SELECT device_id, hostname, os, address, substr(token_hash, 1, 6)
            FROM device_pairings WHERE approved = 0 AND last_seen >= ? ORDER BY hostname
        """, (time.time() - PAIRING_TIMEOUT_SECONDS,)).fetchall()
    return [{"id": device_id, "hostname": hostname, "os": os_name, "address": address, "code": code.upper()}
            for device_id, hostname, os_name, address, code in rows]


def approve_pairing(device_id):
    path = telemetry_db_path()
    if not path.is_file():
        return False
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        if not _pairing_table_exists(db):
            return False
        result = db.execute("""
            UPDATE device_pairings SET approved = 1 WHERE device_id = ? AND (approved = 1 OR last_seen >= ?)
        """, (device_id, time.time() - PAIRING_TIMEOUT_SECONDS))
        return result.rowcount == 1


def list_devices():
    path = telemetry_db_path()
    if not path.is_file():
        return []
    with closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True, timeout=2)) as db:
        rows = db.execute("SELECT device_id, address, received_at, sample FROM devices ORDER BY device_id").fetchall()

    expiry = time.time() - OFFLINE_AFTER_SECONDS
    return [
        {"id": device_id, "address": address, "last_seen": received_at,
         "online": received_at >= expiry, "metrics": json.loads(sample)}
        for device_id, address, received_at, sample in rows
    ]


def create_publisher_invitation(address):
    path = telemetry_db_path()
    if not path.is_file():
        return None
    secret = secrets.token_urlsafe(32)
    digest = hashlib.sha256(secret.encode()).hexdigest()
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='publisher_settings'").fetchone()
        if not exists:
            return None
        settings = db.execute("SELECT url FROM publisher_settings").fetchone()
        if not settings:
            return None
        now = time.time()
        db.execute("DELETE FROM publisher_invitations WHERE expires_at < ?", (now,))
        db.execute("DELETE FROM publisher_invitations WHERE address = ?", (address,))
        db.execute("INSERT INTO publisher_invitations VALUES (?, ?, ?, ?)",
                   (digest, f"invite-{digest[:24]}", address, now + INVITATION_LIFETIME))
    return f"{settings[0]}/publisher/{secret}"


def _publisher_bundle(receiver_url, ca_bundle, secret, device_id):
    source = Path(__file__).parent
    contents = BytesIO()
    with ZipFile(contents, 'w', ZIP_DEFLATED) as bundle:
        for name in ('publish_metrics.py', 'device_discovery.py', 'tools/__init__.py', 'tools/metrics.py'):
            bundle.write(source / name, name)
        bundle.writestr('requirements.txt', 'psutil>=5.9\nrequests>=2.31\n')
        settings = {'url': receiver_url, 'secret': secret, 'device_id': device_id}
        if ca_bundle:
            bundle.writestr('receiver-ca.pem', ca_bundle)
            settings['ca_bundle'] = 'receiver-ca.pem'
        bundle.writestr('invitation.json', json.dumps(settings))
        bundle.writestr('README.txt', 'Ratchet publisher\n\n'
                        'Extract all files. In a terminal in this folder, run these commands.\n\n'
                        '  python -m pip install -r requirements.txt\n'
                        '  python publish_metrics.py --invitation invitation.json\n\n'
                        'Use python3 if your system uses that command. The publisher asks for consent\n'
                        'before requesting a connection. Compare the displayed code in Ratchet and click Connect.\n'
                        'Keep the publisher running to report metrics. Press Ctrl+C to stop.\n')
    contents.seek(0)
    return contents


def create_receiver(tokens, *, allow_pairing=False, publisher_url=None, publisher_ca=None):
    if not isinstance(tokens, dict) or (not tokens and not allow_pairing) or any(
        not isinstance(device_id, str) or not DEVICE_ID.fullmatch(device_id)
        or not isinstance(token, str) or not DEVICE_TOKEN.fullmatch(token)
        for device_id, token in tokens.items()
    ) or len(set(tokens.values())) != len(tokens):
        raise ValueError("RATCHET_DEVICE_TOKENS must map device IDs to distinct URL-safe tokens of at least 32 characters")

    path = telemetry_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with closing(sqlite3.connect(path, timeout=2)) as db, db:
        db.execute("""
            CREATE TABLE IF NOT EXISTS devices (
                device_id TEXT PRIMARY KEY,
                address TEXT,
                received_at REAL NOT NULL,
                sample TEXT NOT NULL
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS device_pairings (
                device_id TEXT PRIMARY KEY,
                hostname TEXT NOT NULL,
                os TEXT NOT NULL,
                address TEXT,
                token_hash TEXT NOT NULL,
                last_seen REAL NOT NULL,
                approved INTEGER NOT NULL DEFAULT 0
            )
        """)
        db.execute("""
            CREATE TABLE IF NOT EXISTS publisher_invitations (
                digest TEXT PRIMARY KEY,
                device_id TEXT NOT NULL UNIQUE,
                address TEXT NOT NULL,
                expires_at REAL NOT NULL
            )
        """)
        db.execute("CREATE TABLE IF NOT EXISTS publisher_settings (url TEXT NOT NULL)")
        db.execute("DELETE FROM publisher_settings")
        if allow_pairing and publisher_url:
            db.execute("INSERT INTO publisher_settings VALUES (?)", (publisher_url,))

    receiver = Flask(__name__, static_folder=None)
    receiver.config["MAX_CONTENT_LENGTH"] = 8192

    @receiver.get('/api/v1/health')
    def health():
        return jsonify(service='ratchet-device-receiver')

    def stored_token_matches(device_id, token):
        with closing(sqlite3.connect(path, timeout=2)) as db:
            row = db.execute("SELECT token_hash FROM device_pairings WHERE device_id=? AND approved=1", (device_id,)).fetchone()
        return row is not None and hmac.compare_digest(row[0], hashlib.sha256(token.encode()).hexdigest())

    @receiver.get('/publisher/<secret>')
    def download_publisher(secret):
        if not publisher_url or not request.is_secure or not DEVICE_TOKEN.fullmatch(secret):
            return jsonify(error='Publisher invitation is unavailable'), 404
        digest = hashlib.sha256(secret.encode()).hexdigest()
        with closing(sqlite3.connect(path, timeout=2)) as db:
            invitation = db.execute("""
                SELECT device_id FROM publisher_invitations WHERE digest = ? AND address = ? AND expires_at >= ?
            """, (digest, request.remote_addr, time.time())).fetchone()
        if not invitation:
            return jsonify(error='Open the invitation on the selected device before the link expires'), 404
        response = send_file(_publisher_bundle(publisher_url, publisher_ca, secret, invitation[0]),
                             mimetype='application/zip', as_attachment=True, download_name='ratchet-publisher.zip')
        response.headers['Cache-Control'] = 'no-store'
        response.headers['Referrer-Policy'] = 'no-referrer'
        return response

    @receiver.put("/api/v1/pairings/<device_id>")
    def request_pairing(device_id):
        if not allow_pairing:
            return jsonify(error="Pairing is disabled"), 404
        if not request.is_secure:
            return jsonify(error="Pairing requires HTTPS"), 403
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        if scheme.lower() != "bearer" or not DEVICE_TOKEN.fullmatch(token):
            return jsonify(error="Invalid pairing credential"), 401
        if not DEVICE_ID.fullmatch(device_id) or device_id in tokens:
            return jsonify(error="Device ID is unavailable"), 409
        body = request.get_json(silent=True)
        if not isinstance(body, dict):
            return jsonify(error="Pairing data must be a JSON object"), 400
        hostname, os_name = body.get("hostname"), body.get("os")
        if any(not isinstance(text, str) or not text.strip() or len(text) > 120 or not text.isprintable()
               for text in (hostname, os_name)):
            return jsonify(error="Provide a hostname and OS of 1 to 120 printable characters"), 400

        now = time.time()
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute("DELETE FROM device_pairings WHERE approved=0 AND last_seen < ?", (now - PAIRING_TIMEOUT_SECONDS,))
            row = db.execute("SELECT token_hash, approved FROM device_pairings WHERE device_id=?", (device_id,)).fetchone()
            if INVITED_DEVICE_ID.fullmatch(device_id) and not (row and row[1]):
                secret = request.headers.get('X-Ratchet-Invitation', '')
                invitation = db.execute("""
                    SELECT digest FROM publisher_invitations WHERE device_id = ? AND address = ? AND expires_at >= ?
                """, (device_id, request.remote_addr, now)).fetchone()
                if not invitation or not DEVICE_TOKEN.fullmatch(secret) or not hmac.compare_digest(
                        invitation[0], hashlib.sha256(secret.encode()).hexdigest()):
                    return jsonify(error='Publisher invitation is expired or belongs to another device'), 403
            if row:
                if not hmac.compare_digest(row[0], token_hash):
                    return jsonify(error="Device ID is unavailable"), 409
                db.execute("UPDATE device_pairings SET last_seen=?, address=? WHERE device_id=?",
                           (now, request.remote_addr, device_id))
                if row[1]:
                    return jsonify(status="connected")
            else:
                if db.execute("SELECT count(*) FROM device_pairings WHERE approved=0").fetchone()[0] >= MAX_PENDING_PAIRINGS:
                    return jsonify(error="Too many pending connections"), 429
                db.execute("""
                    INSERT INTO device_pairings (device_id, hostname, os, address, token_hash, last_seen)
                    VALUES (?, ?, ?, ?, ?, ?)
                """, (device_id, hostname.strip(), os_name.strip(), request.remote_addr, token_hash, now))
        return jsonify(status="pending"), 202

    @receiver.put("/api/v1/devices/<device_id>/telemetry")
    def receive_telemetry(device_id):
        scheme, _, token = request.headers.get("Authorization", "").partition(" ")
        expected = tokens.get(device_id)
        if scheme.lower() != "bearer" or not token:
            return jsonify(error="Invalid device credentials"), 401, {"WWW-Authenticate": "Bearer"}
        authorized = hmac.compare_digest(token.encode(), expected.encode()) if expected else stored_token_matches(device_id, token)
        if not authorized:
            return jsonify(error="Invalid device credentials"), 401, {"WWW-Authenticate": "Bearer"}
        if not request.is_json:
            return jsonify(error="Content-Type must be application/json"), 415
        try:
            sample = _normalize_sample(request.get_json(silent=True), device_id)
        except ValueError as error:
            return jsonify(error=str(error)), 400

        address = request.remote_addr
        if not address or _is_loopback(address):
            address = None
        received_at = time.time()
        with closing(sqlite3.connect(path, timeout=2)) as db, db:
            db.execute("""
                INSERT INTO devices (device_id, address, received_at, sample) VALUES (?, ?, ?, ?)
                ON CONFLICT(device_id) DO UPDATE SET address = excluded.address,
                    received_at = excluded.received_at, sample = excluded.sample
                WHERE excluded.received_at >= devices.received_at
            """, (device_id, address, received_at, json.dumps(sample)))
        return jsonify(id=device_id, received_at=received_at)

    @receiver.errorhandler(413)
    def oversized_sample(_error):
        return jsonify(error="Telemetry body exceeds 8 KiB"), 413

    return receiver


def main():
    parser = argparse.ArgumentParser(description="Run Ratchet's device telemetry receiver.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5001)
    parser.add_argument("--cert", help="TLS certificate chain in PEM format")
    parser.add_argument("--key", help="TLS private key in PEM format")
    parser.add_argument("--discover", action="store_true", help="Advertise the receiver and enable click-to-pair")
    parser.add_argument("--publisher-ca", help="Optional public PEM CA to include in publisher downloads")
    args = parser.parse_args()
    if bool(args.cert) != bool(args.key) or (not args.cert and not _is_loopback(args.host)):
        parser.error("Supply both --cert and --key for a non-loopback listener")
    if args.discover and not args.cert:
        parser.error("Discovery requires a TLS certificate and key")
    if args.publisher_ca and not args.discover:
        parser.error("Publisher downloads require --discover")
    try:
        publisher_ca = Path(args.publisher_ca).read_bytes() if args.publisher_ca else None
        if publisher_ca is not None and not PEM_CERTIFICATES.fullmatch(publisher_ca):
            parser.error("--publisher-ca must contain only public PEM certificates")
        discovery = start_discovery(args.host, args.port) if args.discover else None
        publisher_url = f'https://{args.host}:{args.port}' if args.discover else None
        receiver = create_receiver(json.loads(os.environ.get("RATCHET_DEVICE_TOKENS", "{}")),
                                   allow_pairing=args.discover, publisher_url=publisher_url, publisher_ca=publisher_ca)
    except (ValueError, OSError) as error:
        parser.error(str(error))
    tls = (args.cert, args.key) if args.cert else None
    try:
        receiver.run(host=args.host, port=args.port, ssl_context=tls, threaded=True, use_reloader=False,
                     request_handler=ReceiverRequestHandler)
    finally:
        if discovery:
            discovery.close()


if __name__ == "__main__":
    main()
