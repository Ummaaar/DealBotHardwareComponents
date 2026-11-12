# Pour héberger le bot en continu (homelab, VPS Oracle) au lieu de GitHub Actions
FROM python:3.12-slim

RUN useradd --create-home --uid 10001 dealbot \
 && mkdir /data && chown dealbot:dealbot /data
WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py config.yaml ./

USER dealbot
ENV DEALBOT_STATE=/data/state.json \
    PYTHONUNBUFFERED=1

# Vérification toutes les 10 minutes
CMD ["python", "bot.py", "--loop", "600"]
