FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 DATA_DIR=/data
WORKDIR /app
COPY requirements.txt ./
RUN python -m pip install --no-cache-dir -r requirements.txt \
    && useradd --uid 10001 --create-home marketer \
    && mkdir -p /data /backups /app/var \
    && chown -R marketer:marketer /data /backups /app/var
COPY app ./app
USER marketer
ENTRYPOINT ["python", "-m", "app"]
CMD ["run"]
