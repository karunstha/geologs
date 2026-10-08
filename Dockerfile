FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEOLOGS_HOST=0.0.0.0 \
    GEOLOGS_PORT=8765

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[postgres]' \
    && useradd --system --uid 10001 --create-home geologs

USER geologs
EXPOSE 8765
CMD ["geologs", "serve"]
