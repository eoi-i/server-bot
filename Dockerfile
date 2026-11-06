
FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update && apt-get install -y \
    openjdk-17-jre-headless \
    apktool \
    jadx \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py .
COPY templates ./templates

RUN mkdir -p /data

ENV PORT=8080
ENV WORK_DIR=/data
ENV PYTHONUNBUFFERED=1

EXPOSE 8080

CMD ["gunicorn", "--bind", "0.0.0.0:8080", \
     "--workers", "1", "--threads", "4", \
     "--timeout", "1000", "app:app"]
