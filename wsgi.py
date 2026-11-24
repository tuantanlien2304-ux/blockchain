"""Điểm vào HTTP server để chạy app trên Docker, Render, Railway...

Chạy: python wsgi.py
"""
from __future__ import annotations

import os
from http.server import ThreadingHTTPServer

from app import HOST, Handler, init_db


def main() -> None:
    init_db()
    port = int(os.environ.get("PORT", "8000"))
    host = HOST if HOST != "127.0.0.1" else "0.0.0.0"
    print(f"Server dang chay tai http://{host}:{port}")
    ThreadingHTTPServer((host, port), Handler).serve_forever()


if __name__ == "__main__":
    main()
