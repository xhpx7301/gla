FROM python:3.12-alpine

RUN pip install --no-cache-dir maxminddb==2.7.1

WORKDIR /app
CMD ["python3", "/app/xui_exporter.py"]
