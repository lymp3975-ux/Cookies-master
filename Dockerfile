FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1
ENV PIP_NO_CACHE_DIR=1
ENV STORAGE_DIR=/app/archives

RUN apt-get update && apt-get install -y --no-install-recommends \
        p7zip-full \
        unzip \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY cookie_checker_bot.py .

RUN mkdir -p /app/archives

CMD ["python", "cookie_checker_bot.py"]
