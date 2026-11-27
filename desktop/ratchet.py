"""Run the Ratchet dashboard in a native Linux window."""

import argparse
from contextlib import closing
from http.client import HTTPConnection, HTTPException, HTTPSConnection
from ipaddress import IPv4Address
import json
import os
from pathlib import Path
import shutil
import signal
import ssl
import subprocess
import sys
from threading import Event, Lock, Thread
import time
from urllib.parse import urlsplit

import gi

gi.require_version('Gtk', '4.0')
gi.require_version('WebKit', '6.0')
from gi.repository import GLib, Gtk, WebKit


ROOT = Path(__file__).resolve().parents[1]
BUNDLED_PYTHON = ROOT / 'python'
APP_ID = 'local.ratchet.Ratchet'
LOOPBACK = '127.0.0.1'
MARKER = b'name="application-name" content="Ratchet"'


def probe_http(host, port, path, tls=None):
    connection = (HTTPConnection(host, port, timeout=1) if tls is None
                  else HTTPSConnection(host, port, timeout=1, context=tls))
    with closing(connection):
        try:
            connection.request('GET', path)
            response = connection.getresponse()
            return response.status, response.read(2048), response.getheader('Allow', '')
        except ConnectionRefusedError:
            return None
        except (OSError, HTTPException) as error:
            raise RuntimeError(f'Could not check the service on {host}:{port}. {error}') from error


def dashboard_ready(port):
    response = probe_http(LOOPBACK, port, '/')
    if response is None:
        return False
    if response[0] == 200 and MARKER in response[1]:
        return True
    raise RuntimeError(f'Local port {port} is in use by another app. Start Ratchet with --port to choose a different port.')


def backend_command(script, host, port):
    backend = ROOT / 'smartbackend'
    python = backend / '.venv' / 'bin' / 'python'
    if not python.is_file():
        python = sys.executable
    return [python, '-B', backend / script, '--host', host, '--port', str(port)]


def backend_environment():
    env = dict(os.environ)
    if not BUNDLED_PYTHON.is_dir():
        return env
    config_dir = Path(env.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'ratchet'
    state_dir = Path(env.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'ratchet'
    config = config_dir / 'config.yaml'
    if 'RATCHET_CONFIG_PATH' not in env:
        config_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        if not config.exists():
            shutil.copyfile(ROOT / 'smartbackend/config.yaml', config)
        env['RATCHET_CONFIG_PATH'] = str(config)
    env.setdefault('RATCHET_REPORTS_DIR', str(state_dir / 'reports'))
    env['PYTHONPATH'] = os.pathsep.join(filter(None, (str(BUNDLED_PYTHON), env.get('PYTHONPATH'))))
    return env


class DesktopService:
    def __init__(self, name, log_name, command, probe, *, env=None, timeout=20):
        self.name = name
        self.log_name = log_name
        self.command = command
        self.probe = probe
        self.env = env
        self.timeout = timeout
        self._lock = Lock()
        self._stopped = Event()
        self._closed = Event()
        self._process = None

    def start(self):
        # A probe returns False for a free port, None for a loading service, and True when ready.
        status = self.probe()
        if status is True:
            return

        state = Path(os.environ.get('XDG_STATE_HOME') or Path.home() / '.local' / 'state') / 'ratchet'
        state.mkdir(parents=True, exist_ok=True, mode=0o700)
        log = state / self.log_name
        process = None
        if status is False:
            with self._lock, log.open('ab') as output:
                if self._stopped.is_set():
                    return
                process = self._process = subprocess.Popen(
                    [sys.executable, '-B', Path(__file__).with_name('owned_service.py'), str(os.getpid()), *self.command],
                    cwd=ROOT, env=self.env, stdin=subprocess.DEVNULL,
                    stdout=output, stderr=subprocess.STDOUT, start_new_session=True,
                )

        limit = time.monotonic() + self.timeout
        while not self._stopped.wait(0.2):
            if self.probe() is True:
                return
            if process is not None and process.poll() is not None:
                raise RuntimeError(f'{self.name} stopped during startup. See {log}')
            if time.monotonic() >= limit:
                detail = f' See {log}' if process is not None else ''
                raise RuntimeError(f'{self.name} did not become ready in time.{detail}')

    def stop(self):
        self._stopped.set()
        with self._lock:
            process = self._process
            try:
                if process is not None and process.poll() is None:
                    try:
                        os.killpg(process.pid, signal.SIGTERM)
                        process.wait(timeout=3)
                    except subprocess.TimeoutExpired:
                        os.killpg(process.pid, signal.SIGKILL)
                        process.wait()
            except ProcessLookupError:
                process.wait()
            finally:
                self._process = None
                self._closed.set()

    def wait_for_shutdown(self):
        # Linux delivers the parent-death signal when the thread that spawned the service exits.
        if self._process is not None:
            self._closed.wait()


class DashboardServer(DesktopService):
    def __init__(self, port, env=None):
        super().__init__('Ratchet backend', 'backend.log', backend_command('server.py', LOOPBACK, port),
                         lambda: dashboard_ready(port), env=env)


def model_service():
    url = urlsplit(os.environ.get('LLAMA_CPP_BASE_URL', 'http://127.0.0.1:8080/v1'))
    if url.scheme != 'http' or url.hostname not in (LOOPBACK, 'localhost') or url.path.rstrip('/') != '/v1':
        return None
    port = url.port or 80

    def probe():
        response = probe_http(LOOPBACK, port, '/health')
        if response is None:
            return False
        code, body, _ = response
        try:
            payload = json.loads(body)
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            if code == 200 and payload.get('status') == 'ok':
                return True
            if code == 503 and isinstance(payload.get('error'), dict) and payload['error'].get('type') == 'unavailable_error':
                return None  # An existing llama.cpp is still loading the model.
        raise RuntimeError(f'Port {port} did not return llama.cpp health.')

    llama = ROOT / 'llama.cpp'
    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share') / 'ratchet'
    model = ROOT / 'smartbackend/models/llama3.2.gguf'
    if BUNDLED_PYTHON.is_dir():
        model = data / 'models/llama3.2.gguf'
    model = Path(os.environ.get('RATCHET_MODEL_PATH') or model).expanduser()
    command = [
        llama / 'build/bin/llama-server', '--model', model,
        '--alias', 'llama3.2', '--host', LOOPBACK, '--port', str(port), '--ctx-size', '2048',
        '--parallel', '1', '--threads', '4', '--n-gpu-layers', '0', '--flash-attn', 'on',
        '--batch-size', '128', '--ubatch-size', '64', '--cache-ram', '0',
        '--no-repack', '--no-warmup', '--sleep-idle-seconds', '30',
        '--cache-type-k', 'q8_0', '--cache-type-v', 'q8_0', '--jinja', '--no-ui',
        '--chat-template-file', llama / 'models/templates/meta-llama-Llama-3.2-3B-Instruct.jinja',
    ]
    return DesktopService('llama.cpp', 'llama.cpp.log', command, probe, timeout=180)


def receiver_settings():
    config = Path(os.environ.get('XDG_CONFIG_HOME') or Path.home() / '.config') / 'ratchet' / 'receiver.json'
    if config.is_file():
        try:
            settings = json.loads(config.read_text(encoding='utf-8'))
            if not isinstance(settings, dict):
                raise ValueError('Expected a JSON object')
            for field in ('cert', 'key', 'ca', 'telemetry_db'):
                if field in settings:
                    if not isinstance(settings[field], str) or not settings[field].strip():
                        raise ValueError(f'{field} must be a path')
                    settings[field] = str(config.parent / Path(settings[field]).expanduser())
            return settings
        except (OSError, ValueError) as error:
            raise RuntimeError(f'Could not read receiver settings at {config}. {error}') from error
    if os.environ.get('RATCHET_DEVICE_TOKENS'):
        return {}
    raise RuntimeError(f'Set up the device receiver in {config}.')


def receiver_service(settings, dashboard_env):
    host = str(IPv4Address(settings.get('host', LOOPBACK)))
    port = settings.get('port', 5001)
    discover = settings.get('discover', False)
    cert, key, ca = (settings.get(field) for field in ('cert', 'key', 'ca'))
    if type(port) is not int or not 1 <= port <= 65535 or not isinstance(discover, bool):
        raise ValueError('Set an IPv4 address, a valid port, and a boolean discover setting for the receiver.')
    if bool(cert) != bool(key) or (not cert and (not IPv4Address(host).is_loopback or discover or ca)):
        raise ValueError('Configure both a TLS certificate and key for a network receiver.')
    command = backend_command('device_telemetry.py', host, port)
    if cert:
        command.extend(['--cert', cert, '--key', key])
    if discover:
        command.append('--discover')
        if ca:
            command.extend(['--publisher-ca', ca])

    env = dashboard_env.copy()
    if 'device_tokens' in settings:
        env['RATCHET_DEVICE_TOKENS'] = json.dumps(settings['device_tokens'])

    tls = ssl.create_default_context(cafile=ca) if cert else None

    def probe():
        response = probe_http(host, port, '/api/v1/health', tls)
        if response is None:
            return False
        code, body, _ = response
        if code == 200 and b'"service":"ratchet-device-receiver"' in body:
            return True
        if code == 404:
            legacy = probe_http(host, port, '/api/v1/devices/check/telemetry', tls)
            if legacy and legacy[0] == 405 and 'PUT' in legacy[2].split(', '):
                return True
        raise RuntimeError(f'{host}:{port} did not return Ratchet device receiver health.')

    return DesktopService('Device receiver', 'device-receiver.log', command, probe, env=env)


class RatchetApplication(Gtk.Application):
    def __init__(self, port):
        super().__init__(application_id=APP_ID)
        self.url = f'http://{LOOPBACK}:{port}/'
        env = backend_environment()
        self.service_errors = []
        self.services = []
        try:
            settings = receiver_settings()
            if 'telemetry_db' in settings:
                env['RATCHET_TELEMETRY_DB'] = settings['telemetry_db']
            self.services.append(receiver_service(settings, env))
        except (RuntimeError, ValueError, OSError) as error:
            self.service_errors.append(f'Device receiver: {error}')
        try:
            if model := model_service():
                self.services.append(model)
        except ValueError as error:
            self.service_errors.append(f'llama.cpp: {error}')
        self.server = DashboardServer(port, env)

    def do_activate(self):
        if window := self.get_active_window():
            window.present()
            return

        window = Gtk.ApplicationWindow(application=self, title='Ratchet')
        window.set_default_size(1280, 820)
        status = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12,
                         halign=Gtk.Align.CENTER, valign=Gtk.Align.CENTER)
        spinner = Gtk.Spinner(spinning=True, halign=Gtk.Align.CENTER)
        label = Gtk.Label(label='Starting Ratchet…', wrap=True, max_width_chars=55,
                          justify=Gtk.Justification.CENTER, selectable=True)
        status.append(spinner)
        status.append(label)
        window.set_child(status)
        window.present()

        banner = Gtk.Label(wrap=True, selectable=True, xalign=0,
                           margin_start=12, margin_end=12, margin_top=6, margin_bottom=6)
        pending = {service.name for service in self.services}

        def update_services():
            progress = [f'Starting {name}…' for name in sorted(pending)]
            messages = progress + self.service_errors
            banner.set_text('\n'.join(messages))
            banner.set_visible(bool(messages))

        def service_finished(name, error):
            pending.discard(name)
            if error:
                self.service_errors.append(f'{name}: {error}')
            update_services()

        def start_service(service):
            error_message = None
            try:
                service.start()
            except (RuntimeError, OSError) as error:
                service.stop()
                error_message = str(error)
            GLib.idle_add(service_finished, service.name, error_message)
            service.wait_for_shutdown()

        def start_dashboard():
            try:
                self.server.start()
            except (RuntimeError, OSError) as error:
                self.server.stop()
                GLib.idle_add(show_error, str(error))
            else:
                GLib.idle_add(show_dashboard)
                self.server.wait_for_shutdown()

        def show_error(message):
            spinner.set_visible(False)
            label.set_text(message)

        def show_dashboard():
            view = WebKit.WebView.new()
            view.set_vexpand(True)
            view.connect('decide-policy', self._navigation_policy)

            def load_failed(_view, _event, uri, error):
                if uri == self.url:
                    window.set_child(status)
                    show_error(f'Could not load Ratchet. {error.message}')
                    return True
                return False

            view.connect('load-failed', load_failed)
            content = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            content.append(banner)
            content.append(view)
            window.set_child(content)
            view.load_uri(self.url)

        update_services()
        for service in self.services:
            Thread(target=start_service, args=(service,), daemon=True).start()
        Thread(target=start_dashboard, daemon=True).start()

    def _navigation_policy(self, _view, decision, kind):
        if kind == WebKit.PolicyDecisionType.NEW_WINDOW_ACTION:
            decision.ignore()
            return True
        if kind == WebKit.PolicyDecisionType.NAVIGATION_ACTION:
            uri = decision.get_navigation_action().get_request().get_uri()
            if not uri.startswith(self.url):
                decision.ignore()
                return True
        return False

    def do_shutdown(self):
        self.server.stop()
        for service in self.services:
            service.stop()
        Gtk.Application.do_shutdown(self)


def install_launcher(port):
    def exec_arg(value):
        escaped = ''.join(f'\\{char}' if char in '\\"`$' else char for char in str(value))
        return '"' + escaped.replace('\\', '\\\\').replace('%', '%%') + '"'

    data = Path(os.environ.get('XDG_DATA_HOME') or Path.home() / '.local' / 'share')
    applications = data / 'applications'
    applications.mkdir(parents=True, exist_ok=True)
    launcher = applications / f'{APP_ID}.desktop'
    executable = (ROOT.parent.parent / 'bin/ratchet',) if BUNDLED_PYTHON.is_dir() else (ROOT / 'ratchet', '--menu')
    command = ' '.join(exec_arg(value) for value in (*executable, '--port', port))
    icon = str(ROOT / 'frontend' / 'ratchet-logo.png').replace('\\', '\\\\')
    launcher.write_text(
        '[Desktop Entry]\n'
        'Name=Ratchet\n'
        'Comment=Monitor edge devices and talk with Doc\n'
        'Type=Application\n'
        f'Exec={command}\n'
        f'Icon={icon}\n'
        'Terminal=false\n'
        'StartupNotify=true\n'
        f'StartupWMClass={APP_ID}\n'
        'Categories=System;Monitor;\n',
        encoding='utf-8',
    )
    print(launcher)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install', action='store_true', help='Add Ratchet to the Linux app menu.')
    parser.add_argument('--port', type=int, default=5000, help='Local Ratchet backend port.')
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error('--port must be between 1 and 65535')
    if args.install:
        install_launcher(args.port)
        return 0

    app = RatchetApplication(args.port)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGTERM, app.quit)
    GLib.unix_signal_add(GLib.PRIORITY_DEFAULT, signal.SIGINT, app.quit)
    return app.run([APP_ID])


if __name__ == '__main__':
    sys.exit(main())
