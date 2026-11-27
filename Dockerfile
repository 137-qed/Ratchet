FROM python:3.12-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y --no-install-recommends \
    procps openssh-client \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements and install Python dependencies
COPY smartbackend/requirements.txt /app/smartbackend/requirements.txt
RUN pip install --no-cache-dir -r /app/smartbackend/requirements.txt

# Copy application code
COPY smartbackend/server.py smartbackend/device_telemetry.py smartbackend/device_discovery.py smartbackend/publish_metrics.py smartbackend/lan_devices.py smartbackend/ssh_metrics.py smartbackend/config.yaml /app/smartbackend/
COPY smartbackend/agent/ /app/smartbackend/agent/
COPY smartbackend/tools/ /app/smartbackend/tools/
COPY frontend/ /app/frontend/

EXPOSE 5000

WORKDIR /app/smartbackend

CMD ["python3", "server.py"]
