# Python 3.10, NOT 3.11. ib_insync 0.9.86 (the only version, project
# unmaintained since 2022) was designed for 3.10. Python 3.11 made
# contextvars.Context.run() raise on re-entry — which ib_insync's sync
# wrappers + nest_asyncio depend on. Result on 3.11: spammed
# `RuntimeError: cannot enter context: ... is already entered`,
# breaking every socket read callback. nest_asyncio 1.6.0+ has a
# partial workaround but it's not sufficient given our threading
# pattern (background reconnect thread + APScheduler + main loop).
# 3.10 allows the re-entry pattern and Just Works.
FROM python:3.10-slim

WORKDIR /app

# System dependencies
# - gcc/g++ for ib_insync + numpy compilation
# - tzdata for timezone support (US/Eastern used throughout bot)
# - curl for container healthchecks (replaces python requests in hc)
ENV DEBIAN_FRONTEND=noninteractive
ENV TZ=US/Eastern
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc g++ tzdata curl && \
    ln -fs /usr/share/zoneinfo/US/Eastern /etc/localtime && \
    dpkg-reconfigure --frontend noninteractive tzdata && \
    rm -rf /var/lib/apt/lists/*

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Create data/logs directories
RUN mkdir -p data logs

# Expose dashboard port
EXPOSE 5000

# Health check
HEALTHCHECK --interval=30s --timeout=10s --retries=3 \
    CMD python -c "import requests; requests.get('http://localhost:5000/health')" || exit 1

# Run the bot
CMD ["python", "run.py"]
