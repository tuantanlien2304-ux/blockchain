"""Điểm vào WSGI để chạy app trên server web (gunicorn, Docker, Render, Railway...).

Chạy cục bộ:      gunicorn -w 1 -b 0.0.0.0:8000 wsgi:application
Chạy như cũ:      python app.py
"""
from __future__ import annotations

import os

from app import HOST, Handler, init_db

init_db()

# Tên "application" là quy ước của gunicorn/WSGI
application = Handler
app = Handler  # một số nền tảng tìm biến tên "app"


if __name__ == "__main__":
    from http.server import ThreadingHTTPServer

    port = int(os.environ.get("PORT", "8000"))
    print(f"Server dang chay tai http://0.0.0.0:{port}")
    ThreadingHTTPServer((HOST if HOST != "127.0.0.1" else "0.0.0.0", port), Handler).serve_forever()
