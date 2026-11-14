FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_SYSTEM_PYTHON=1

# OCR for scanned PDFs (app/lectura): Tesseract with Spanish, English, French and orientation data.
RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       tesseract-ocr tesseract-ocr-spa tesseract-ocr-eng tesseract-ocr-fra tesseract-ocr-osd \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:0.8.22 /uv /usr/local/bin/uv

WORKDIR /app

# Install dependencies first (cached layer), then the code (mounted in dev).
COPY pyproject.toml ./
RUN uv pip install -r pyproject.toml --extra dev --extra ui

COPY . .

EXPOSE 8000 8501
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
