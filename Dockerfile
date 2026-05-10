FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        sqlite3 tini \
    && rm -rf /var/lib/apt/lists/*

RUN useradd --create-home --shell /bin/bash --uid 1000 mailtriage

WORKDIR /app

COPY pyproject.toml ./
COPY src ./src
RUN pip install .

COPY config ./config
COPY scripts ./scripts

RUN mkdir -p /var/lib/mailtriage \
    && chown -R mailtriage:mailtriage /var/lib/mailtriage /app

USER mailtriage
EXPOSE 8088

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "mailtriage"]
