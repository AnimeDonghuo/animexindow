FROM python:3.11-slim-bookworm

# Install FFMPEG, FFprobe and critical runtime utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    tzdata \
    git \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Docker CLI + compose plugin (static binaries - the daemon itself stays on the
# host and is reached through the mounted socket). Needed by /update.
ARG DOCKER_VER=27.3.1
ARG COMPOSE_VER=v2.29.7
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    case "$arch" in \
      amd64) darch=x86_64; carch=x86_64 ;; \
      arm64) darch=aarch64; carch=aarch64 ;; \
      *) echo "unsupported arch $arch" >&2; exit 1 ;; \
    esac; \
    curl -fsSL "https://download.docker.com/linux/static/stable/${darch}/docker-${DOCKER_VER}.tgz" \
      | tar -xz -C /tmp docker/docker; \
    mv /tmp/docker/docker /usr/local/bin/docker; \
    rm -rf /tmp/docker; \
    mkdir -p /usr/local/lib/docker/cli-plugins; \
    curl -fsSL -o /usr/local/lib/docker/cli-plugins/docker-compose \
      "https://github.com/docker/compose/releases/download/${COMPOSE_VER}/docker-compose-linux-${carch}"; \
    chmod +x /usr/local/bin/docker /usr/local/lib/docker/cli-plugins/docker-compose; \
    docker --version; docker compose version

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure standard output/error logs show up immediately in docker logs
ENV PYTHONUNBUFFERED=1

# Keep image/BLAS libs from spawning threads on a 1 vCPU box
ENV OMP_NUM_THREADS=1

CMD ["python", "bot.py"]
