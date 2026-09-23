FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=3000

RUN apt-get update && apt-get install -y --no-install-recommends \
    ca-certificates \
    dos2unix \
    sqlite3 \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

RUN dos2unix ./start.sh && chmod +x ./start.sh

# Stato persistente (SQLite, registro posizioni, portafoglio paper)
RUN mkdir -p /app/data

EXPOSE 3000

CMD ["/bin/bash", "./start.sh"]
