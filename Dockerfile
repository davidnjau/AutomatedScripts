FROM python:3.11-slim

WORKDIR /app

# Install dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Copy source
COPY ardhisasa_auth.py .
COPY bot.py .

# Non-root user for safety
RUN useradd -m botuser && mkdir -p /app/data && chown botuser:botuser /app/data
USER botuser

CMD ["python", "-u", "bot.py"]