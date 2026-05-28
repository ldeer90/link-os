FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt pytest

COPY . .
RUN mkdir -p data generated/search_harvest generated/guest_post_prospecting generated/instantly_uploads

CMD ["python", "-c", "print('Prospecting container ready. Run a tool command with docker compose run --rm prospecting python tools/...')"]
