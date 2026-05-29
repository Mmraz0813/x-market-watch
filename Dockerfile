FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV WEB_HOST=0.0.0.0
ENV WEB_PORT=8787
ENV WEB_AUTO_POLL=true
ENV X_MARKET_WATCH_ENV_PATH=data/.env

WORKDIR /app

COPY pyproject.toml README.md ./
COPY src ./src

RUN pip install --no-cache-dir .

CMD ["x-market-watch", "daemon"]
