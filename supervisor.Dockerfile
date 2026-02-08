# Supervisor-based Dockerfile (for non-K8s / single-container runs).
# With K8s, use the main Dockerfile and run API and workers as separate deployments.
#
# Use Python as the base image
FROM python:3.10-slim

# Set work directory
WORKDIR /app

# Install system dependencies (includes supervisor)
RUN apt-get update && apt-get install -y \
    supervisor \
    gcc \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt .

# Install Python dependencies
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code
COPY . .

# Create logs directory for supervisord
RUN mkdir -p /app/logs

# Copy supervisor configuration
COPY supervisord.conf /etc/supervisor/conf.d/supervisord.conf

# Create non-root user for security (optional)
# RUN useradd --create-home --shell /bin/bash app
# RUN chown -R app:app /app
# USER app

# Expose FastAPI port
EXPOSE 8003

# Start supervisor to manage processes (e.g. FastAPI, Celery)
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]
