# Manual startup and deployment

For the standard Linux desktop launch, see the [desktop quick start](../README.md#quick-start).

## Start the dashboard and model separately

### 1. Install Dependencies

```bash
pip install -r smartbackend/requirements.txt
```

### 2. Start the Local Model

Place the Llama 3.2 GGUF weights at `smartbackend/models/llama3.2.gguf` and run the compiled server from the project root.

```bash
./llama.cpp/build/bin/llama-server \
  --model smartbackend/models/llama3.2.gguf --alias llama3.2 \
  --host 127.0.0.1 --port 8080 --ctx-size 4096 --parallel 1 --threads 4 \
  --n-gpu-layers 0 --flash-attn on --cache-type-k q8_0 --cache-type-v q8_0 \
  --jinja --no-ui \
  --chat-template-file llama.cpp/models/templates/meta-llama-Llama-3.2-3B-Instruct.jinja
```

Ratchet uses `http://127.0.0.1:8080/v1` by default. Set `LLAMA_CPP_BASE_URL` to override it and `LLAMA_CPP_API_KEY` if you configured server authentication. The `--alias` must match the selected local model name in `smartbackend/config.yaml`.

### 3. Start Ratchet

```bash
python3 smartbackend/server.py
```

### 4. Open the Dashboard

Navigate to **http://localhost:5000/** in your browser.

The dashboard will show:
- **Real-time CPU, Memory, Disk** metrics from your machine
- **Self-healing agent activity** — anomalies detected, actions taken
- **Sparkline charts** with live updating metrics
- **Event log** of system events and agent decisions

## Manual device setup

### Start the HTTPS pairing receiver

On the Ratchet host, use the Wi-Fi address covered by the certificate. The optional `--publisher-ca` argument adds the public CA certificate to publisher downloads.

```bash
python3 smartbackend/device_telemetry.py --discover --host 192.168.1.10 \
  --cert /path/to/receiver.crt --key /path/to/receiver.key \
  --publisher-ca /path/to/public-ca.pem
```

### Start the publisher manually over Wi-Fi

The receiver's `--discover` flag also enables UDP discovery on port `45871`. To run the publisher manually, copy `smartbackend/publish_metrics.py`, `smartbackend/device_discovery.py`, the `smartbackend/tools/` directory, and the receiver's public certificate or CA bundle to the device. Install `psutil` and `requests`, then run the publisher on the same Wi-Fi.

```bash
python3 publish_metrics.py --discover --ca-bundle receiver.crt
```

Open the local Ratchet dashboard and click **Connect** beside the matching device and six-character code under **Nearby Devices**. Its first reading moves it into **Edge Devices**. The publisher saves its pairing credentials in its user profile and can reconnect when run again. If the network filters broadcasts, use `--pair --url https://192.168.1.10:5001 --ca-bundle receiver.crt` to reach the same pairing screen.

### Start the receiver on the Ratchet host

Generate a token with `python3 -c 'import secrets; print(secrets.token_urlsafe(32))'`. Set `RATCHET_DEVICE_TOKENS` to a JSON object that maps each device ID to its unique token, for example `{"sensor-01":"<generated-token>"}`. IDs use lowercase letters, digits, hyphens, and underscores.

For direct network access, bind the receiver to a device-facing address and provide a TLS certificate matching its hostname or IP address.

```bash
python3 smartbackend/device_telemetry.py --host 192.168.1.10 \
  --cert /path/to/fullchain.pem --key /path/to/privkey.pem
```

For an existing SSH connection, you can start the receiver with `python3 smartbackend/device_telemetry.py` on port `5001` and run `ssh -N -L 5001:127.0.0.1:5001 user@ratchet-host` on the device. Use `http://127.0.0.1:5001` as the publisher URL through that tunnel. Keep the dashboard on its existing private endpoint.

The receiver and dashboard use `smartbackend/reports/device-telemetry.sqlite3`. If you set `RATCHET_TELEMETRY_DB`, use the same database path for both processes. A dashboard running with Compose can read the default database through the existing reports mount.

### Publish from the device

Copy `smartbackend/publish_metrics.py`, `smartbackend/device_discovery.py`, and the `smartbackend/tools/` directory onto the device, preserving their relative paths. Install `psutil` and `requests` in the Python environment used by the publisher. Set `RATCHET_DEVICE_TOKEN` to the token assigned to that device, then run:

```bash
python3 smartbackend/publish_metrics.py --url https://ratchet.example.net:5001 --device-id sensor-01
```

The publisher sends a sample every 10 seconds. Use `--once` to check a connection or `--ca-bundle /path/to/ca.pem` to validate a receiver certificate issued by a private CA.

## Docker

```bash
docker compose up --build
```

Run Compose from the project root. The container uses `http://host.docker.internal:8080/v1` for llama.cpp by default. Point `LLAMA_CPP_BASE_URL` at a `llama-server` address reachable from the container if your deployment uses a different address.

## Build the Ubuntu package

Run the [source setup](../README.md#quick-start) first to prepare the local Python environment and build llama.cpp. Install a C compiler, `dpkg-deb`, and `patchelf`, then run the package script from the project root.

```bash
./packaging/build-deb
```

The script writes a `.deb` to `dist/`. It stages a standard Linux menu entry, the dashboard and backend, the Python runtime dependencies at their tested versions, and the llama.cpp executable and shared libraries. It rewrites the bundled libraries to load from their installed directory. The package registers an AppArmor profile for the Ratchet launcher so WebKit can create its sandbox on Ubuntu. Model weights, TLS keys, receiver tokens and device records stay in your user directories.

