FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    ATTENDANCE_DATA_DIR=/data \
    ATTENDANCE_HOST=0.0.0.0 \
    PORT=10000

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt gunicorn

COPY . .
RUN mkdir -p /data

EXPOSE 10000

# 1 worker để SQLite và phiên đăng nhập (lưu trong RAM) nhất quán
CMD ["sh", "-c", "gunicorn --workers 1 --threads 8 --bind 0.0.0.0:${PORT} wsgi:application"]
