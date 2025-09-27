FROM python:3.11-slim

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    libopenexr-dev \
    libopencolorio-dev \
    && rm -rf /var/lib/apt/lists/*

# Set working directory
WORKDIR /app

# Copy dependency manifests
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application source code
COPY app/ ./app/
COPY main_api.py .
COPY storage/config.ocio ./storage/config.ocio

# Create required directories
RUN mkdir -p storage/conv storage/temp

# Ensure entrypoint is executable
RUN chmod +x main_api.py

# Expose API port
EXPOSE 8000

# Application entrypoint
CMD ["python", "main_api.py"] 
