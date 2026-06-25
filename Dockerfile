FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY anonproxy ./anonproxy
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

# vault lives here; mount a volume to persist mappings across restarts
ENV ANONPROXY_VAULT_DIR=/data/vaults
VOLUME ["/data/vaults"]

EXPOSE 8080
# bind to all interfaces *inside* the container; compose maps it to 127.0.0.1 only
ENV HOST=0.0.0.0
CMD ["python", "-m", "anonproxy", "serve"]
