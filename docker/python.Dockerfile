FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PYTHONPATH=/app/src
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY src/app ./src/app
COPY src/shared ./src/shared
COPY src/worker ./src/worker
COPY src/optimizer ./src/optimizer
COPY alembic.ini .
COPY src/db ./src/db
