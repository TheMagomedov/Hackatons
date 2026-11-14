FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home marketer \
    && mkdir -p /data /backups /app/var \
    && chown -R marketer:marketer /data /backups /app/var
COPY app ./app
USER marketer
ENTRYPOINT ["python", "-m", "app"]
CMD ["run"]
