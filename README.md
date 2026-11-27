
https://github.com/user-attachments/assets/9c9dd531-868f-4f15-b78b-14e4d037e404

# Ratchet — Self-Healing Edge Device Supervisor

An autonomous agent that continuously monitors edge device health, detects anomalies, diagnoses root causes, and takes self-healing actions — all while streaming real-time data to a sleek dashboard.

https://github.com/user-attachments/assets/e59c9af1-37c0-43ce-af74-17421e1596ac


## Architecture

```
smartbackend/
├── tools/
│   ├── metrics.py      # CPU, memory, disk, temp, network via psutil
│   ├── logs.py         # journalctl, dmesg, syslog reader
│   ├── actions.py      # restart_service, kill_process, clear_cache, reboot
│   └── alerts.py       # Telegram, email, JSON reports
├── agent/
│   └── supervisor_agent.py   # Anomaly detection + diagnosis + auto-healing loop
├── server.py           # Flask REST API + SSE real-time streaming
├── config.yaml         # Thresholds, intervals, alert settings
└── requirements.txt
```

## Quick start

On Ubuntu, install `python3-venv`, `python3-gi`, `gir1.2-gtk-4.0`, and `gir1.2-webkit-6.0`. If you still need to compile llama.cpp, also install `cmake` and `build-essential`. Place your Llama 3.2 GGUF at `smartbackend/models/llama3.2.gguf` or configure an existing OpenAI-compatible llama.cpp endpoint with `LLAMA_CPP_BASE_URL`.

Run from the project root.

```bash
./ratchet setup
./ratchet
```

Run setup once to install the Python dependencies, build llama.cpp if needed, and add **Ratchet** to the Linux app menu. After that, open **Ratchet** from the menu or run `./ratchet`.

The desktop app starts the dashboard, llama.cpp, and the configured device receiver. It reuses services that are already running and stops the ones it started when you close the window. The dashboard is also available at [127.0.0.1:5000](http://127.0.0.1:5000/). The default desktop model uses a 2,048-token context and releases model memory after 30 seconds idle. Startup logs are in `~/.local/state/ratchet/`.

See [manual startup and deployment](docs/manual-start.md) for running the services separately, setting up a publisher by hand, or using Docker.

## Install the Ubuntu app

Open `ratchet_0.1.2_amd64.deb` in Ubuntu App Center, install it, then open **Ratchet** from the app menu. The package targets Ubuntu 24.04 amd64 and includes the dashboard, Python dependencies, and compiled llama.cpp. Place your Llama 3.2 GGUF at `~/.local/share/ratchet/models/llama3.2.gguf`, or set `RATCHET_MODEL_PATH` to an existing model file.

The installed app reads its editable config and receiver settings from `~/.config/ratchet/`. Device samples and reports go to `~/.local/state/ratchet/reports/`. The [package build instructions](docs/manual-start.md#build-the-ubuntu-package) describe how to build another copy.

The [publishing guide](docs/publishing.md) covers publishing the source on GitHub, attaching a prebuilt `.deb`, and installing a release from the terminal.

## Configure the Wi-Fi receiver

To start a Wi-Fi receiver with the desktop app, put its existing TLS settings in `~/.config/ratchet/receiver.json` (or `$XDG_CONFIG_HOME/ratchet/receiver.json`). Paths can be relative to this file. Keep the settings and certificate private key in your local configuration directory.

```json
{
  "host": "192.168.1.10",
  "port": 5001,
  "discover": true,
  "cert": "receiver.crt",
  "key": "receiver.key",
  "ca": "receiver.crt"
}
```

Use the Wi-Fi IP covered by your certificate. The `ca` field is optional when clients already trust the issuing CA. Add `device_tokens` with the existing device-to-token map if you use pre-enrolled publishers. Add `telemetry_db` only if you use a custom device database. An existing `RATCHET_DEVICE_TOKENS` environment setting can also start a loopback receiver without a settings file.

## Connect an external device

Open **Nearby Devices** in the dashboard on the Linux Ratchet host. Ratchet discovers IPv4 neighbors on its Wi-Fi interface and displays their hostnames when the local resolver has them. Click a device to choose **Send publisher** or **SSH connection**. Use **Refresh** to check again. The optional `RATCHET_LAN_INTERFACE` environment variable selects a specific interface, such as a wired interface connected to the same LAN.

The first valid reading adds the device to **Edge Devices**. Subsequent readings update its metrics, and Ratchet marks the device offline after 45 seconds without an update.

### Send publisher

The desktop app starts the HTTPS receiver using the Wi-Fi settings above. The target browser needs to trust the receiver certificate.

Click **Send publisher**, copy the invitation link, and open it on the selected device within 15 minutes. Wi-Fi discovery provides the address. The device owner opens the link and chooses whether to run the publisher. Extract the ZIP and follow `README.txt`. When the publisher displays a six-character code, compare it with the one under **Nearby Devices** and click **Connect** beside the matching request.

### SSH connection

Click **SSH connection** and enter the Linux device's SSH username and port. Enter the device's SSH password or leave it blank to use the Ratchet host's OpenSSH client and keys. If the host key is saved in `known_hosts` under a hostname, enter it in **Saved SSH hostname**. Ratchet verifies the key before authentication and polls CPU, memory, disk and uptime with read-only commands. A password connection reuses the authenticated SSH session and requires a new login after it drops. Click **Disconnect SSH** to stop polling.

### Send telemetry from custom firmware

Send `PUT /api/v1/devices/sensor-01/telemetry` to the receiver with `Authorization: Bearer <device-token>` and `Content-Type: application/json`. The required fields are `cpu_percent`, `memory_percent`, and `disk_percent`, each between 0 and 100. Optional fields include `hostname`, `os`, `uptime_seconds`, `temperature_celsius`, `network_connections`, and `process_count`. Unavailable optional measurements can be `null`.

```json
{"hostname":"Workshop sensor","cpu_percent":12.4,"memory_percent":37.1,"disk_percent":24.8,"temperature_celsius":42.0}
```

## API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/metrics` | GET | Current system metrics |
| `/api/devices` | GET | Latest external device samples and heartbeat status |
| `/api/metrics/history` | GET | Historical metrics (last 60 readings) |
| `/api/logs` | GET | Recent system logs |
| `/api/agent/status` | GET | Agent status (running, cycles, anomalies) |
| `/api/agent/events` | GET | Agent event log |
| `/api/agent/anomalies` | GET | Detected anomalies |
| `/api/agent/config` | GET/PUT | Read/update agent config |
| `/api/actions/restart` | POST | Restart a service |
| `/api/actions/kill` | POST | Kill a process |
| `/api/actions/clear-cache` | POST | Clear system caches |
| `/api/chat` | POST | Ask Doc a question with optional live streaming |
| `/api/stream` | GET | SSE real-time metric stream. Add `?devices=1` to include external devices |

Send `"stream": true` in the chat request body to receive newline-delimited JSON. Each line contains a `delta` string, a `done` flag, or an `error` string. Standard chat requests return a JSON object with the complete `reply`.

## Configuration

Edit `config.yaml` to customize:

- **Thresholds**: CPU, memory, disk, temperature warning/critical levels
- **Interval**: How often the agent checks (default: 60s)
- **Auto-heal**: Enable/disable autonomous remediation
- **Alerts**: Telegram bot and email SMTP settings

## How It Works

Every 60 seconds, the supervisor agent:

1. **Collects** real system metrics via psutil
2. **Detects** anomalies using threshold + z-score analysis
3. **Reads** system logs for error context
4. **Diagnoses** root cause via rule-based reasoning
5. **Acts** — kills runaway processes, clears caches, restarts services
6. **Reports** — writes JSON reports, sends Telegram/email alerts

## Self-Healing Examples

| Anomaly | Diagnosis | Action |
|---------|-----------|--------|
| CPU > 95% for 3 cycles | Runaway process identified | `kill_process(pid)` |
| Memory > 95% + growing | Memory leak in service | `kill_process(pid)` |
| Disk > 95% | Disk nearly full | `clear_cache()` |
| Temperature > 85°C | Overheating | Monitor + alert |
