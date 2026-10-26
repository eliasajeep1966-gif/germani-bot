FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# Install dependencies in a dedicated layer for reproducible rebuilds.
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

# Copy application code and the tracked B1 catalog/guides.
COPY . .

# Runtime-writable state is kept outside the image and mounted by Compose.
RUN mkdir -p /var/lib/bot \
    && useradd --create-home --uid 10001 --shell /usr/sbin/nologin botuser \
    && chown -R botuser:botuser /app /var/lib/bot

USER botuser

CMD ["python", "-B", "main.py"]
