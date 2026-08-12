FROM python:3.10-slim-buster

# Install FFMPEG, FFprobe and critical runtime utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Install Python requirements
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application files
COPY . .

# Ensure standard output/error logs show up immediately in docker logs
ENV PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
