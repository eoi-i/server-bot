FROM python:3.12-slim

ENV DEBIAN_FRONTEND=noninteractive
ENV JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64
ENV PATH="/opt/jadx/bin:${PATH}"

# Install Java 21 and APKTool
RUN apt-get update && apt-get install -y \
    openjdk-21-jre-headless \
    apktool \
    curl \
    unzip \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install JADX directly from official GitHub releases
ENV JADX_VERSION=1.5.6

RUN mkdir -p /opt/jadx && \
    curl -fL \
    "https://github.com/skylot/jadx/releases/download/v${JADX_VERSION}/jadx-${JADX_VERSION}.zip" \
    -o /tmp/jadx.zip && \
    unzip -q /tmp/jadx.zip -d /opt/jadx && \
    chmod +x /opt/jadx/bin/jadx && \
    rm /tmp/jadx.zip && \
    jadx --version

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

CMD ["gunicorn", \
     "--bind", "0.0.0.0:8080", \
     "--workers", "1", \
     "--threads", "4", \
     "--timeout", "1000", \
     "app:app"]
