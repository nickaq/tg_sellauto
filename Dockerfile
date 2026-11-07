FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# UID 1000 должен совпадать с владельцем ./data на сервере (см. README)
RUN useradd --create-home --uid 1000 bot \
    && mkdir -p data \
    && chown bot:bot data
USER bot

CMD ["python", "bot.py"]
