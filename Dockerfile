FROM python:3.11-slim

# Make pip more resilient to transient network issues during image builds.
ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_DEFAULT_TIMEOUT=180 \
    PIP_RETRIES=15 \
    PIP_PROGRESS_BAR=off

# ffmpeg is used to compress preview videos that exceed the Telegram size limit.
RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency manifests
COPY requirements.txt .

# Install Python dependencies
RUN python -m pip install --upgrade pip setuptools wheel && \
    attempts=0; \
    until [ "$attempts" -ge 4 ]; do \
        python -m pip install --no-cache-dir --prefer-binary -r requirements.txt && break; \
        attempts=$((attempts + 1)); \
        echo "pip install failed (attempt $attempts/4); retrying..."; \
        sleep $((attempts * 10)); \
    done; \
    [ "$attempts" -lt 4 ]

# Copy application source code
COPY app/ ./app/
COPY scripts/ ./scripts/
COPY main.py .
COPY data/config.ocio ./data/config.ocio

# Create required directories
RUN mkdir -p data/conv data/temp

# Ensure entrypoint is executable
RUN chmod +x main.py

# Expose preview-upload endpoint port (if enabled)
EXPOSE 8081

# Application entrypoint
CMD ["python", "main.py"]
