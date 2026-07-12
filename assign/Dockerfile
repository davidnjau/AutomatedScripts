FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source files
COPY bot.py .
COPY common.py .
COPY dlv_core.py .
COPY fetch_tasks_cache.py .
COPY dlv_batch.py .
COPY dlv_tasks.py .
COPY morning_briefing.py .
COPY fetch_tasks.py .
COPY email_service.py .
COPY telegram_report.py .
COPY refresh_auth.py .
COPY sectional_properties.py .
COPY auto_fetch.py .
COPY receive_tasks.py .
COPY token_rotator.py .
COPY lookup_reference.py .
COPY ardhisasa_auth.py .
COPY token_refresh_daemon.py .

# Non-root user for safety
RUN useradd -m botuser && mkdir -p /app/data && chown botuser:botuser /app/data
USER botuser

CMD ["python", "-u", "bot.py"]