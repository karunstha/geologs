FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEOTRACKER_HOST=0.0.0.0 \
    GEOTRACKER_PORT=8765

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir '.[postgres]' \
    && useradd --system --uid 10001 --create-home geotracker

USER geotracker
EXPOSE 8765
CMD ["geotracker", "serve"]
