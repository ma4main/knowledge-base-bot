FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

WORKDIR /app/bot

# git и ssh нужны боту для коммитов и push; tzdata, чтобы TZ из compose применялся.
RUN apt-get update \
    && apt-get install -y --no-install-recommends git openssh-client tzdata \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# База знаний монтируется томом, а не копируется в образ: git pull подхватывается
# перезапуском контейнера без пересборки.
ENV KB_ROOT=/app/kb

# Проверяется не «процесс жив», а «Telegram отвечает»: отметку обновляет
# heartbeat.py после успешного get_me(), healthcheck.py смотрит на её свежесть.
HEALTHCHECK --interval=60s --timeout=10s --start-period=90s --retries=3 \
    CMD ["python", "healthcheck.py"]
CMD ["python", "main.py"]
