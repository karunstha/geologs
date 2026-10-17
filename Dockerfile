# syntax=docker/dockerfile:1
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    GEOLOGS_HOST=0.0.0.0 \
    GEOLOGS_PORT=8765

RUN useradd --system --uid 10001 --create-home geologs
WORKDIR /app

# Dependencies come first, from a stub of the package, so this layer is rebuilt only when
# pyproject.toml changes and not on every edit to the code.
COPY pyproject.toml README.md ./
RUN --mount=type=cache,target=/root/.cache/pip \
    mkdir -p src/geologs && touch src/geologs/__init__.py \
    && pip install '.[postgres]' \
    && pip uninstall -y geologs \
    && rm -rf build src

# The app itself: a quick layer, rebuilt on each code change.
COPY src ./src
RUN --mount=type=cache,target=/root/.cache/pip pip install --no-deps .

USER geologs
EXPOSE 8765
CMD ["geologs", "serve"]
