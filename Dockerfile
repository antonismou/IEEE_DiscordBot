FROM python:3.12-slim
ENV TZ=Europe/Athens
RUN apt-get update && apt-get install -y --no-install-recommends tzdata \
    && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot ./bot
COPY scripts ./scripts
RUN useradd --create-home --uid 1000 bot \
    && mkdir -m 700 /app/data && chown bot:bot /app/data
USER bot
CMD ["python", "-m", "bot.main"]
