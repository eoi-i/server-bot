FROM python:3.12-slim
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 DEBIAN_FRONTEND=noninteractive JAVA_HOME=/usr/lib/jvm/java-21-openjdk-amd64 PATH=/opt/jadx/bin:/usr/local/bin:/usr/bin:/bin
RUN apt-get update && apt-get install -y --no-install-recommends openjdk-21-jre-headless apktool curl unzip ca-certificates && rm -rf /var/lib/apt/lists/*
ARG JADX_VERSION=1.5.6
RUN mkdir -p /opt/jadx && curl -fsSL https://github.com/skylot/jadx/releases/download/v${JADX_VERSION}/jadx-${JADX_VERSION}.zip -o /tmp/jadx.zip && unzip -q /tmp/jadx.zip -d /opt/jadx && rm /tmp/jadx.zip && chmod +x /opt/jadx/bin/jadx
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
RUN mkdir -p /data/uploads /data/work /data/results
EXPOSE 8080
CMD ["gunicorn","--bind=0.0.0.0:8080","--workers=1","--threads=4","--timeout=1900","--graceful-timeout=30","app:app"]
