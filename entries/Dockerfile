FROM python:3.11-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py .
COPY ardhisasa_auth.py .

RUN useradd -m botuser && mkdir -p /app/data && chown botuser:botuser /app/data
USER botuser

CMD ["python", "-u", "bot.py"]
