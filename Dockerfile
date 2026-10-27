# Use Python as the base image
FROM python:3.15-slim

# Set work directory
WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    supervisor \
    gcc \
    g++ \
    libyaml-dev \
    make \
    python3-dev \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Copy requirements first for better Docker layer caching
COPY requirements.txt requirements-build.lock ./

# Hash-lock build tools before disabling isolation for the two source builds.
RUN python -m pip install --no-cache-dir --only-binary=:all: --require-hashes -r requirements-build.lock
# These two packages do not publish CPython 3.15 wheels yet.
RUN python -m pip install --no-cache-dir --no-build-isolation \
    --only-binary=:all: --no-binary=marisa-trie,pyyaml \
    -r requirements.txt
RUN python -m pip check && python -c \
    "import marisa_trie, pandas, pydantic, rapidfuzz, zope.interface; from yaml import CLoader; from langcodes import Language; assert Language.get('hi').display_name('en') == 'Hindi'"

# Bake the scheme-search embedding model into the image. Prod has no egress to
# huggingface.co, so downloading it lazily on first search_schemes call fails
# with "Can't load the model for 'intfloat/multilingual-e5-large'".
RUN python -c "from sentence_transformers import SentenceTransformer; SentenceTransformer('intfloat/multilingual-e5-large')"

# Model is cached in the layer above — never reach for the Hub at runtime.
ENV HF_HUB_OFFLINE=1

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

# Start supervisor to manage both FastAPI and Celery
CMD ["/usr/bin/supervisord", "-c", "/etc/supervisor/conf.d/supervisord.conf"]