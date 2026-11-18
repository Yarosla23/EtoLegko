FROM python:3.15.2-slim

WORKDIR /app
COPY speedtest.py .
COPY etolegko/ ./etolegko/
USER 10001:10001

ENTRYPOINT ["python", "-B", "/app/speedtest.py"]
