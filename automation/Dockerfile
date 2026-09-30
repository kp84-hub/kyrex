FROM python:3.11-slim
RUN groupadd --gid 10001 watcher && useradd --uid 10001 --gid 10001 --no-create-home watcher \
    && mkdir /data && chown watcher:watcher /data
WORKDIR /app
COPY automation/email_watcher.py /app/email_watcher.py
USER 10001:10001
CMD ["python", "-u", "/app/email_watcher.py"]
