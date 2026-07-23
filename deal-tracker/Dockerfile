FROM node:22-alpine AS frontend

WORKDIR /build/web
COPY web/package.json web/package-lock.json ./
RUN npm ci
COPY web/ ./
RUN npm run build


FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY deal-tracker/requirements.txt /tmp/deal-tracker-requirements.txt
COPY prospecting/requirements.txt /tmp/prospecting-requirements.txt
RUN pip install --no-cache-dir -r /tmp/deal-tracker-requirements.txt -r /tmp/prospecting-requirements.txt pytest

COPY deal-tracker /app/deal-tracker
COPY prospecting /app/prospecting
COPY --from=frontend /build/web/dist /app/web/dist
WORKDIR /app/deal-tracker
ENV PYTHONPATH=/app/deal-tracker:/app/prospecting
RUN mkdir -p data generated/imports generated/comparisons /app/prospecting/data /app/prospecting/generated

EXPOSE 8766

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8766"]
