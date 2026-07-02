# =============================================================================
# BuckGen — Minimal Agent Deployment
# Base: python:3.11-slim (Render-compatible)
# =============================================================================

FROM python:3.11-slim

# Prevent Python from writing .pyc files
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install system deps (ccxt + web3 need some libs)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    && rm -rf /var/lib/apt/lists/*

# Copy only requirements first for Docker layer caching
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy application
COPY . .

# Expose the FastAPI port
EXPOSE 8000

# Start with uvicorn (Render free tier health check expects HTTP on $PORT)
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
