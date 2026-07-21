FROM python:3.11-slim

# Install ffmpeg — required for merging video+audio streams
RUN apt-get update && apt-get install -y --no-install-recommends \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Railway injects $PORT at runtime — gunicorn must bind to it
CMD gunicorn app:app --bind 0.0.0.0:${PORT:-8080} --timeout 300 --workers 1
