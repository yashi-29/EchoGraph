FROM python:3.12-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends libpq5 tesseract-ocr tesseract-ocr-eng \
    && rm -rf /var/lib/apt/lists/*

FROM base AS builder
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential libpq-dev \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt .
RUN pip install --user -r requirements.txt

FROM base AS runtime
RUN useradd --create-home --uid 1000 appuser
COPY --from=builder /root/.local /home/appuser/.local
COPY app ./app
COPY alembic ./alembic
COPY alembic.ini .
RUN chown -R appuser:appuser /app /home/appuser/.local
ENV PATH=/home/appuser/.local/bin:$PATH
USER appuser

EXPOSE 8000
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
