FROM python:3.12-slim
ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 \
    PATH="/opt/jadx/bin:/usr/local/bin:/usr/bin:/bin"
RUN apt-get update && apt-get install -y --no-install-recommends \
    openjdk-21-jre-headless apktool curl unzip ca-certificates \
    && rm -rf /var/lib/apt/lists/*
ARG JADX_VERSION=1.5.6
RUN mkdir -p /opt/jadx && curl -fL "https://github.com/skylot/jadx/releases/download/v${JADX_VERSION}/jadx-${JADX_VERSION}.zip" -o /tmp/jadx.zip \
    && unzip -q /tmp/jadx.zip -d /opt/jadx \
    && rm /tmp/jadx.zip \
    && chmod +x /opt/jadx/bin/jadx
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /tmp/apk-analyzer-data
EXPOSE 8080
CMD ["gunicorn", "--bind", "0.0.0.0:8080", "--workers", "1", "--threads", "4", "--timeout", "1800", "app:app"]
