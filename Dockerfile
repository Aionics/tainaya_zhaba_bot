FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    PIP_NO_CACHE_DIR=1

# tzdata — чтобы напоминания в 18:00 приходили по нужному часовому поясу (TZ в .env)
RUN apt-get update \
    && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt
COPY bot.py .

# База и резервные копии лежат в /app/data — эта папка подключается с диска сервера
VOLUME ["/app/data"]
ENV DB_PATH=/app/data/zhabilizacia.db

CMD ["python", "bot.py"]
