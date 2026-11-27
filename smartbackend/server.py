"""
Ratchet — Edge Agent Flask Server
Serves the dashboard API, SSE stream, LLM model management, and chat endpoints.
"""

import argparse
import os
import sqlite3
import sys
import json
import time
import yaml
import logging
import threading
from contextlib import closing
from urllib.parse import urlsplit

from flask import Flask, jsonify, request, Response, send_from_directory
from flask_cors import CORS

# Add parent dirs to path so tools/ and agent/ are importable
sys.path.insert(0, os.path.dirname(__file__))

from tools.metrics import get_system_metrics
from tools.actions import restart_service, kill_process, clear_cache
from tools.logs import read_recent_logs
from agent.supervisor_agent import run_agent_cycle, chat_with_agent, stream_chat_with_agent
from device_telemetry import approve_pairing, create_publisher_invitation, list_devices, list_pending_pairings
from lan_devices import NearbyDevices
from ssh_metrics import SSHMetrics

# ── Logging ──
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(name)s] %(levelname)s: %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S',
)
logger = logging.getLogger('ratchet.server')

# ── Config ──
CONFIG_PATH = os.environ.get('RATCHET_CONFIG_PATH', os.path.join(os.path.dirname(__file__), 'config.yaml'))

def load_config():
    try:
        with open(CONFIG_PATH) as f:
            return yaml.safe_load(f) or {}
    except Exception:
        return {}

def save_config(cfg):
    try:
        with open(CONFIG_PATH, 'w') as f:
            yaml.dump(cfg, f, default_flow_style=False, sort_keys=False)
    except Exception as e:
        logger.warning(f"Could not save config: {e}")

config = load_config()

# ── Flask App ──
DASHBOARD_DIR = os.path.join(os.path.dirname(__file__), '..', 'frontend')

app = Flask(__name__, static_folder=DASHBOARD_DIR, static_url_path='')
CORS(app)
nearby_devices = NearbyDevices()
ssh_metrics = SSHMetrics()

# ── In-memory state ──
agent_events = []
anomalies = []
agent_state = {
    'state': 'running',
    'auto_heal': config.get('agent', {}).get('auto_heal', True),
    'total_events': 0,
    'anomaly_counters': {'total': 0},
}


# ═══════════════════════════════════════════════════════
#  STATIC ROUTES
# ═══════════════════════════════════════════════════════

@app.route('/')
def serve_index():
    return send_from_directory(DASHBOARD_DIR, 'index.html')


# ═══════════════════════════════════════════════════════
#  METRICS API
# ═══════════════════════════════════════════════════════

@app.route('/api/metrics')
def api_metrics():
    metrics = get_system_metrics()
    return jsonify(metrics)

@app.route('/api/devices')
def api_devices():
    try:
        return jsonify(devices=list_devices() + ssh_metrics.devices())
    except sqlite3.Error:
        logger.exception('Could not read the external device registry')
        return jsonify({'error': 'Device telemetry is temporarily unavailable'}), 503


def local_pairing_request():
    dashboard_origin = request.host_url.rstrip('/')
    return (request.remote_addr in ('127.0.0.1', '::1', '::ffff:127.0.0.1')
            and urlsplit(dashboard_origin).hostname in ('127.0.0.1', '::1', 'localhost')
            and request.origin in (None, dashboard_origin)
            and request.headers.get('Sec-Fetch-Site') in (None, 'same-origin', 'none'))


@app.get('/api/devices/nearby')
def api_nearby_devices():
    if not local_pairing_request():
        return jsonify(error='Open the local Ratchet dashboard to connect devices'), 403
    try:
        peers, scanning, error = nearby_devices.snapshot(refresh=request.args.get('refresh') == '1')
        ssh_devices = {device['address']: device['metrics']['hostname'] for device in ssh_metrics.devices()}
        for peer in peers:
            if hostname := ssh_devices.pop(peer['address'], None):
                peer.update(ssh=True, hostname=hostname)
        peers.extend(dict(address=address, hostname=hostname, ssh=True) for address, hostname in ssh_devices.items())
        return jsonify(devices=list_pending_pairings(), peers=peers, scanning=scanning, discovery_error=error)
    except sqlite3.Error:
        logger.exception('Could not read nearby device requests')
        return jsonify(error='Nearby devices are temporarily unavailable'), 503


@app.post('/api/devices/nearby/<device_id>/connect')
def api_connect_device(device_id):
    if not local_pairing_request():
        return jsonify(error='Open the local Ratchet dashboard to connect devices'), 403
    try:
        if approve_pairing(device_id):
            return jsonify(id=device_id, status='connected')
        return jsonify(error='The device stopped advertising. Start its publisher and try again.'), 404
    except sqlite3.Error:
        logger.exception('Could not approve device %s', device_id)
        return jsonify(error='Device pairing is temporarily unavailable'), 503


@app.post('/api/devices/nearby/<address>/publisher')
def api_invite_publisher(address):
    if not local_pairing_request():
        return jsonify(error='Open the local Ratchet dashboard to connect devices'), 403
    if not nearby_devices.contains(address):
        return jsonify(error='This device is no longer in the Wi-Fi discovery results. Refresh and try again.'), 404
    try:
        if url := create_publisher_invitation(address):
            return jsonify(url=url), 201
        return jsonify(error='Start Ratchet’s HTTPS telemetry receiver with --discover to send a publisher.'), 503
    except sqlite3.Error:
        logger.exception('Could not create an invitation for %s', address)
        return jsonify(error='Publisher invitations are temporarily unavailable'), 503


@app.route('/api/devices/nearby/<address>/ssh', methods=['POST', 'DELETE'])
def api_connect_ssh(address):
    if not local_pairing_request():
        return jsonify(error='Open the local Ratchet dashboard to connect devices'), 403
    if request.method == 'DELETE':
        if ssh_metrics.disconnect(address):
            return jsonify(status='disconnected')
        return jsonify(error='There is no SSH connection to this device.'), 404
    if not nearby_devices.contains(address):
        return jsonify(error='This device is no longer in the Wi-Fi discovery results. Refresh and try again.'), 404
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(error='Enter an SSH username and port.'), 400
    try:
        device_id = ssh_metrics.connect(address, body.get('username'), body.get('port', 22),
                                        password=body.get('password'), trusted_host=body.get('trusted_host'))
        return jsonify(id=device_id, status='connected')
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except OSError:
        logger.exception('Could not start SSH for %s', address)
        return jsonify(error='The SSH client could not start.'), 503


@app.route('/api/logs')
def api_logs():
    lines = request.args.get('lines', 50, type=int)
    since = request.args.get('since', 5, type=int)
    logs = read_recent_logs(lines=lines, since_minutes=since)
    return jsonify({'logs': logs})


# ═══════════════════════════════════════════════════════
#  AGENT STATUS & EVENTS
# ═══════════════════════════════════════════════════════

@app.route('/api/agent/status')
def api_agent_status():
    return jsonify(agent_state)

@app.route('/api/agent/events')
def api_agent_events():
    limit = request.args.get('limit', 20, type=int)
    return jsonify(agent_events[:limit])


# ═══════════════════════════════════════════════════════
#  ACTIONS API
# ═══════════════════════════════════════════════════════

@app.route('/api/actions/restart', methods=['POST'])
def api_action_restart():
    data = request.get_json() or {}
    service = data.get('service_name')
    if not service:
        return jsonify({'error': 'Missing service_name'}), 400
    res = restart_service(service)
    return jsonify(res)

@app.route('/api/actions/kill', methods=['POST'])
def api_action_kill():
    data = request.get_json() or {}
    pid = data.get('pid')
    if not pid:
        return jsonify({'error': 'Missing pid'}), 400
    res = kill_process(pid)
    return jsonify(res)

@app.route('/api/actions/clear-cache', methods=['POST'])
def api_action_clear_cache():
    res = clear_cache()
    return jsonify(res)


# ═══════════════════════════════════════════════════════
#  LLM MODEL MANAGEMENT
# ═══════════════════════════════════════════════════════

@app.route('/api/llm/models', methods=['GET'])
def api_llm_models():
    cfg = load_config()
    models = cfg.get('llm_models', [])
    return jsonify({'models': models})


@app.route('/api/llm/models/active', methods=['PUT'])
def api_set_active_model():
    data = request.get_json() or {}
    model_name = data.get('model')
    if not model_name:
        return jsonify({'error': 'Missing model name'}), 400

    cfg = load_config()
    models = cfg.get('llm_models', [])
    found = False
    for m in models:
        if m['name'] == model_name:
            m['active'] = True
            found = True
        else:
            m['active'] = False

    if not found:
        return jsonify({'error': f'Model "{model_name}" not found'}), 404

    cfg['llm_models'] = models
    save_config(cfg)
    return jsonify({'success': True, 'active_model': model_name})


@app.route('/api/llm/apikey', methods=['POST'])
def api_set_apikey():
    """Store an API key and mark cloud models as connected."""
    data = request.get_json() or {}
    api_key = data.get('api_key', '').strip()
    if not api_key:
        return jsonify({'error': 'Missing api_key'}), 400

    # Store in environment for the agent to pick up
    os.environ['OPENAI_API_KEY'] = api_key

    # Mark cloud models as connected
    cfg = load_config()
    models = cfg.get('llm_models', [])
    for m in models:
        provider = m.get('provider', '').lower()
        if 'local' not in provider and 'meta' not in provider:
            m['status'] = 'connected'
    cfg['llm_models'] = models
    save_config(cfg)

    logger.info("API key configured, cloud models marked as connected")
    return jsonify({'success': True, 'models': models})


# ═══════════════════════════════════════════════════════
#  CHAT API (Talk with Doc)
# ═══════════════════════════════════════════════════════

@app.route('/api/chat', methods=['POST'])
def api_chat():
    """Process a chat message using the configured LLM provider (real reasoning)."""
    data = request.get_json() or {}
    message = data.get('message', '').strip()
    history = data.get('history', [])
    if not message:
        return jsonify({'error': 'Missing message'}), 400

    if data.get('stream') is True:
        return Response(chat_events(message, history), mimetype='application/x-ndjson',
                        headers={'Cache-Control': 'no-cache, no-transform', 'X-Accel-Buffering': 'no'})

    try:
        cfg = load_config()
        reply = chat_with_agent(message, history, cfg)
        return jsonify({'reply': reply})
    except Exception as e:
        logger.error(f"Chat error: {e}", exc_info=True)
        return jsonify({'reply': f'⚠️ LLM error: {str(e)}'})


def chat_events(message, history):
    """Send each chat delta as it becomes available from the model."""
    try:
        with closing(stream_chat_with_agent(message, history, load_config())) as reply:
            for text in reply:
                yield json.dumps({'delta': text}) + '\n'
    except Exception:
        logger.exception('Chat stream failed')
        yield json.dumps({'error': 'Doc could not finish the reply. Try again.'}) + '\n'
    else:
        yield json.dumps({'done': True}) + '\n'


# ═══════════════════════════════════════════════════════
#  SERVER-SENT EVENTS (SSE)
# ═══════════════════════════════════════════════════════

def sse_stream(include_devices=False):
    """Generate SSE events with local metrics and optional external readings."""
    while True:
        try:
            metrics = get_system_metrics()
        except Exception as e:
            logger.error(f"SSE local metrics error: {e}")
            if not include_devices:
                time.sleep(5)
                continue
            metrics = None

        payload = metrics
        if include_devices:
            payload = {'metrics': metrics}
            try:
                payload['devices'] = list_devices() + ssh_metrics.devices()
            except sqlite3.Error:
                logger.exception('Could not stream external device metrics')
        yield f"data: {json.dumps(payload, default=str)}\n\n"
        time.sleep(3)

@app.route('/api/stream')
def api_stream():
    return Response(sse_stream(request.args.get('devices') == '1'), mimetype='text/event-stream',
                    headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


# ═══════════════════════════════════════════════════════
#  BACKGROUND AGENT LOOP
# ═══════════════════════════════════════════════════════

def agent_loop():
    """Background thread: collects metrics every interval and runs the LLM agent cycle.
    Falls back to simple threshold logging if the LLM is unavailable.
    """
    interval = config.get('agent', {}).get('interval_seconds', 60)
    thresholds = config.get('thresholds', {})

    while True:
        try:
            cfg = load_config()  # Reload config each cycle so live edits take effect
            metrics = get_system_metrics()

            cpu  = metrics.get('cpu', {}).get('percent', 0)
            mem  = metrics.get('memory', {}).get('percent', 0)
            disk = metrics.get('disk', {}).get('percent', 0)
            logger.info(f"[METRICS] CPU: {cpu}% | MEM: {mem}% | DISK: {disk}%")

            # ── Intelligent agent cycle via LLM ──
            try:
                result = run_agent_cycle(metrics, cfg)

                for evt in result.get('events', []):
                    agent_events.insert(0, {
                        'type':    evt.get('severity', 'info'),
                        'message': evt.get('message', ''),
                        'time':    time.time(),
                    })
                    agent_state['total_events'] += 1

                for anomaly in result.get('anomalies', []):
                    anomalies.insert(0, anomaly)
                    agent_state['anomaly_counters']['total'] += 1

                if result.get('actions'):
                    logger.info(f"[AGENT] {len(result['actions'])} action(s) taken by LLM")

            except Exception as e:
                logger.warning(f"[AGENT] LLM cycle failed, falling back to threshold checks: {e}")
                _threshold_check(metrics, thresholds)

            # Trim event/anomaly lists
            while len(agent_events) > 100:
                agent_events.pop()
            while len(anomalies) > 50:
                anomalies.pop()

        except Exception as e:
            logger.error(f"Agent loop error: {e}")

        time.sleep(interval)


def _threshold_check(metrics, thresholds):
    """Simple rule-based fallback when no LLM is available."""
    cpu  = metrics.get('cpu', {}).get('percent', 0)
    mem  = metrics.get('memory', {}).get('percent', 0)
    disk = metrics.get('disk', {}).get('percent', 0)

    if cpu > thresholds.get('cpu_warning', 85):
        agent_events.insert(0, {'type': 'warning', 'message': f'CPU at {cpu}%', 'time': time.time()})
        agent_state['total_events'] += 1

    if mem > thresholds.get('memory_warning', 80):
        agent_events.insert(0, {'type': 'warning', 'message': f'Memory at {mem}%', 'time': time.time()})
        agent_state['total_events'] += 1

    if disk > thresholds.get('disk_warning', 85):
        agent_events.insert(0, {'type': 'warning', 'message': f'Disk at {disk}%', 'time': time.time()})
        agent_state['total_events'] += 1


# ═══════════════════════════════════════════════════════
#  MAIN
# ═══════════════════════════════════════════════════════

if __name__ == '__main__':
    srv = config.get('server', {})
    parser = argparse.ArgumentParser(description='Run the Ratchet dashboard server.')
    parser.add_argument('--host', default=srv.get('host', '0.0.0.0'))
    parser.add_argument('--port', type=int, default=srv.get('port', 5000))
    args = parser.parse_args()

    # Start agent background thread
    agent_thread = threading.Thread(target=agent_loop, daemon=True)
    agent_thread.start()
    logger.info("Ratchet agent started in background")

    app.run(
        host=args.host,
        port=args.port,
        debug=srv.get('debug', False),
        threaded=True,
        use_reloader=False,
    )
