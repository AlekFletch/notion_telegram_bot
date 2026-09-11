FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

# ffmpeg нужен yt-dlp, когда сайт отдаёт видео и звук раздельными
# потоками — без него такой пост скачается без звука или не скачается.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY bot ./bot
COPY scripts ./scripts

# На Render всегда вебхук: свободного фонового воркера на free-плане нет.
ENV MODE=webhook

CMD ["python", "-m", "bot.main"]
