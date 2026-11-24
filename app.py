from __future__ import annotations

import base64
import binascii
import csv
import hashlib
import hmac
import io
import json
import os
import secrets
import sqlite3
import stat
import time
import unicodedata
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import NamedTuple, TypedDict
from urllib.parse import parse_qs, urlparse

import qrcode
import qrcode.image.svg
import zxingcpp
from PIL import Image, UnidentifiedImageError

ROOT = Path(__file__).resolve().parent
HOST = os.environ.get("ATTENDANCE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ATTENDANCE_PORT", os.environ.get("PORT", "8000")))
BASE_URL = os.environ.get("ATTENDANCE_BASE_URL", f"http://localhost:{PORT}").rstrip("/")
# Cho phép suy ra URL công khai từ request (dùng khi deploy web mà chưa đặt ATTENDANCE_BASE_URL)
PUBLIC_URL_FROM_REQUEST = os.environ.get("ATTENDANCE_PUBLIC_URL_FROM_REQUEST", "").strip().lower() in (
    "1",
    "true",
    "yes",
    "on",
)
ALLOWED_HOSTS = [h.strip().lower() for h in os.environ.get("ATTENDANCE_ALLOWED_HOSTS", "").split(",") if h.strip()]


def _writable_dir(candidate: Path) -> bool:
    """Kiểm tra thư mục có ghi được không (filesystem serverless thường chỉ-đọc)."""
    try:
        candidate.mkdir(parents=True, exist_ok=True)
        probe = candidate / f".write-test-{os.getpid()}"
        probe.write_bytes(b"ok")
        probe.unlink()
        return True
    except OSError:
        return False


# Thư mục dữ liệu: ưu tiên ATTENDANCE_DATA_DIR, rồi thư mục dự án (Windows), rồi /tmp (Linux/serverless)
if os.environ.get("ATTENDANCE_DATA_DIR"):
    DATA_DIR = Path(os.environ["ATTENDANCE_DATA_DIR"])
else:
    DATA_DIR = next(
        (p for p in (ROOT, Path("/tmp/attendance-data")) if _writable_dir(p)),
        ROOT,
    )

# Serverless (Vercel/Netlify/AWS Lambda) không giữ được SQLite trên đĩa tạm
EPHEMERAL_MODE = os.environ.get("ATTENDANCE_EPHEMERAL", "").strip().lower() in ("1", "true", "yes", "on")
if EPHEMERAL_MODE:
    DATA_DIR = Path(os.environ.get("ATTENDANCE_DATA_DIR", "/tmp/attendance-data"))
    _writable_dir(DATA_DIR)

DB_PATH = Path(os.environ.get("ATTENDANCE_DB", DATA_DIR / "attendance.db"))
SECRET_PATH = Path(os.environ.get("ATTENDANCE_SECRET", DATA_DIR / ".demo-secret"))
TOKEN_LIFETIME = 30 * 60
QR_IMAGE_MAX_BYTES = 4 * 1024 * 1024
QR_IMAGE_MAX_JSON_BYTES = ((QR_IMAGE_MAX_BYTES + 2) // 3) * 4 + 128
QR_IMAGE_MAX_PIXELS = 16_000_000
DEMO_MODE = os.environ.get("ATTENDANCE_DEMO", "").strip().lower() in ("1", "true", "yes", "on")

# SQL khóa bất biến: luôn khôi phục bằng một chỗ duy nhất
TRIGGER_NO_UPDATE_SQL = """
CREATE TRIGGER IF NOT EXISTS attendance_no_update
BEFORE UPDATE ON attendance_events
BEGIN SELECT RAISE(ABORT, 'attendance history is append-only'); END;
"""


class SessionRecord(TypedDict):
    username: str
    effective_username: str


SESSIONS: dict[str, SessionRecord] = {}
GENESIS_HASH = "0" * 64


def _text(row: dict[str, object], key: str) -> str:
    return str(row.get(key) or "")


class UserRecord(NamedTuple):
    effective_username: str
    username: str
    role: str
    name: str
    password: str = ""
    student_id: str = ""
    alias: str = ""
    class_name: str = ""
    wallet_address: str = ""
    created_at: str = ""

    @classmethod
    def from_row(cls, effective_username: str, row: dict[str, object]) -> "UserRecord":
        return cls(
            effective_username=effective_username,
            username=_text(row, "username"),
            role=_text(row, "role"),
            name=_text(row, "name"),
            password=_text(row, "password"),
            student_id=_text(row, "student_id"),
            alias=_text(row, "alias"),
            class_name=_text(row, "class_name"),
            wallet_address=_text(row, "wallet_address"),
            created_at=_text(row, "created_at"),
        )

    def get(self, key: str) -> str:
        """Tương thích với mã cũ truy cập dạng dict."""
        return str(getattr(self, key, ""))


def connect() -> sqlite3.Connection:
    class ClosingConnection(sqlite3.Connection):
        def __exit__(self, exc_type, exc_value, traceback):
            try:
                return super().__exit__(exc_type, exc_value, traceback)
            finally:
                self.close()

    db = sqlite3.connect(DB_PATH, timeout=15, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    db.execute("PRAGMA busy_timeout = 15000")  # tránh lỗi 'database is locked' khi chạy nhiều worker
    return db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_secret() -> bytes:
    env_secret = os.environ.get("ATTENDANCE_SECRET_KEY", "").strip()
    if env_secret:
        return env_secret.encode("utf-8")
    if not SECRET_PATH.exists():
        # Tạo nguyên tử để hai luồng không sinh ra hai khóa khác nhau
        SECRET_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(SECRET_PATH, os.O_CREAT | os.O_EXCL | os.O_WRONLY, stat.S_IRUSR | stat.S_IWUSR)
        except FileExistsError:
            pass  # Luồng khác đã tạo trước đó
        else:
            with os.fdopen(fd, "wb") as handle:
                handle.write(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()


def generate_wallet(identifier: str) -> str:
    digest = hashlib.sha256(identifier.encode("utf-8")).hexdigest()
    return "0x" + digest[:40]


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            -- BẢNG LƯU TRỮ TÀI KHOẢN VÀ THÔNG TIN SINH VIÊN / GIẢNG VIÊN
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT NOT NULL UNIQUE,
                alias TEXT,
                password TEXT NOT NULL,
                role TEXT NOT NULL CHECK (role IN ('lecturer', 'student', 'admin')),
                name TEXT NOT NULL,
                student_id TEXT,
                class_name TEXT,
                wallet_address TEXT NOT NULL,
                created_at TEXT NOT NULL,
                list_class_code TEXT
            );
            CREATE INDEX IF NOT EXISTS idx_users_student_id ON users(student_id);
            CREATE INDEX IF NOT EXISTS idx_users_alias ON users(alias);

            -- BẢNG MÔN HỌC VÀ LỚP HỌC
            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                course_code TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                lecturer_username TEXT NOT NULL
            );

            -- BẢNG GHI DANH SINH VIÊN VÀO LỚP
            CREATE TABLE IF NOT EXISTS enrollments (
                class_id INTEGER NOT NULL REFERENCES classes(id),
                student_username TEXT NOT NULL,
                PRIMARY KEY (class_id, student_username)
            );

            -- BẢNG BUỔI HỌC VÀ PHIÊN ĐIỂM DANH
            CREATE TABLE IF NOT EXISTS sessions (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                class_id INTEGER NOT NULL REFERENCES classes(id),
                title TEXT NOT NULL,
                starts_at TEXT NOT NULL,
                ends_at TEXT NOT NULL,
                attendance_code TEXT,
                status TEXT NOT NULL DEFAULT 'open'
                    CHECK (status IN ('open', 'closed'))
            );

            -- BẢNG SỔ CÁI BẤT BIẾN ON-CHAIN (HASH-CHAIN)
            CREATE TABLE IF NOT EXISTS attendance_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                session_id INTEGER NOT NULL REFERENCES sessions(id),
                student_username TEXT NOT NULL,
                actor_username TEXT NOT NULL,
                event_type TEXT NOT NULL CHECK (event_type = 'CHECK_IN'),
                occurred_at TEXT NOT NULL,
                previous_hash TEXT NOT NULL,
                event_hash TEXT NOT NULL,
                UNIQUE (session_id, student_username)
            );

            CREATE TRIGGER IF NOT EXISTS attendance_no_update
            BEFORE UPDATE ON attendance_events
            BEGIN SELECT RAISE(ABORT, 'attendance history is append-only'); END;

            CREATE TRIGGER IF NOT EXISTS attendance_no_delete
            BEFORE DELETE ON attendance_events
            BEGIN SELECT RAISE(ABORT, 'attendance history is append-only'); END;
            """
        )

        # CẬP NHẬT CỘT NẾU CHƯA CÓ
        user_cols = {row["name"] for row in db.execute("PRAGMA table_info(users)").fetchall()}
        if "list_class_code" not in user_cols:
            # Lớp danh sách (roster) mà tài khoản thuộc về — ví dụ CNTT2-K25
            db.execute("ALTER TABLE users ADD COLUMN list_class_code TEXT")

        class_cols = {row["name"] for row in db.execute("PRAGMA table_info(classes)").fetchall()}
        if "course_code" not in class_cols:
            db.execute("ALTER TABLE classes ADD COLUMN course_code TEXT NOT NULL DEFAULT ''")
        db.execute("UPDATE classes SET course_code=code WHERE course_code=''")

        session_cols = {row["name"] for row in db.execute("PRAGMA table_info(sessions)").fetchall()}
        if "attendance_code" not in session_cols:
            db.execute("ALTER TABLE sessions ADD COLUMN attendance_code TEXT")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS sessions_attendance_code_unique "
            "ON sessions(attendance_code) WHERE attendance_code IS NOT NULL"
        )
        for row in db.execute("SELECT id FROM sessions WHERE attendance_code IS NULL").fetchall():
            db.execute("UPDATE sessions SET attendance_code=? WHERE id=?", (new_attendance_code(db), row["id"]))

        # SEED DỮ LIỆU TÀI KHOẢN BAN ĐẦU VÀO DATABASE
        initial_users = [
            {
                "username": "gv01",
                "alias": "le-dac-nhuong",
                "password": "GVdemo123!",
                "role": "lecturer",
                "name": "TS. Lê Đắc Nhường",
                "student_id": "GV01",
                "class_name": "Khoa CNTT - ĐH Hải Phòng",
                "wallet_address": "0x9A48F7d2c4B869271E7D311d95c1103f5C8B49E2",
            },
            {
                "username": "243148201076",
                "alias": "sv01",
                "password": "SVdemo123!",
                "role": "student",
                "name": "Trần Gia Bảo",
                "student_id": "243148201076",
                "class_name": "CNTT2.K25",
                "wallet_address": "0x243148201076EaBc88921Fa32B81e5952B9c011aA",
            },
            {
                "username": "243148201086",
                "alias": "sv02",
                "password": "SVdemo123!",
                "role": "student",
                "name": "Hoàng Lê Minh",
                "student_id": "243148201086",
                "class_name": "CNTT2.K25",
                "wallet_address": "0x243148201086Cd729Ba10d867c2957bA10b349bB",
            },
            {
                "username": "24314820105",
                "alias": "sv03",
                "password": "SVdemo123!",
                "role": "student",
                "name": "Nguyễn Hồng Sơn",
                "student_id": "24314820105",
                "class_name": "CNTT2.K25",
                "wallet_address": "0x243148201051F109aAb007817E231920B9e731cC",
            },
            {
                "username": "243148201072",
                "alias": "sv04",
                "password": "SVdemo123!",
                "role": "student",
                "name": "Đồng Đức Cường",
                "student_id": "243148201072",
                "class_name": "CNTT2.K25",
                "wallet_address": "0x243148201072Bc7124f0A761c7817e92318B49dD",
            },
        ]

        for u in initial_users:
            db.execute(
                """
                INSERT OR IGNORE INTO users (
                    username, alias, password, role, name, student_id, class_name, wallet_address, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    u["username"],
                    u["alias"],
                    u["password"],
                    u["role"],
                    u["name"],
                    u["student_id"],
                    u["class_name"],
                    u["wallet_address"],
                    now_iso(),
                ),
            )

        # KHỞI TẠO LỚP HỌC MẪU
        db.execute(
            """INSERT OR IGNORE INTO classes(code, course_code, name, lecturer_username)
            VALUES(?, ?, ?, ?)""",
            ("CNTT101", "CNTT101", "Nhập môn Công nghệ thông tin", "gv01"),
        )
        class_id = db.execute("SELECT id FROM classes WHERE code = ?", ("CNTT101",)).fetchone()["id"]

        # GHI DANH CÁC SINH VIÊN VÀO LỚP
        for u in initial_users:
            if u["role"] == "student":
                db.execute(
                    "INSERT OR IGNORE INTO enrollments(class_id, student_username) VALUES(?, ?)",
                    (class_id, u["username"]),
                )
                if u.get("alias"):
                    db.execute(
                        "INSERT OR IGNORE INTO enrollments(class_id, student_username) VALUES(?, ?)",
                        (class_id, u["alias"]),
                    )

        # NẠP DANH SÁCH LỚP CNTT2.K25 TỪ students.json (tài khoản = mã SV, mật khẩu = 1)
        seed_class_list(db)

        # TẠO BUỔI HỌC MẪU
        if not db.execute("SELECT 1 FROM sessions WHERE class_id = ?", (class_id,)).fetchone():
            db.execute(
                """INSERT INTO sessions(
                    class_id, title, starts_at, ends_at, attendance_code
                ) VALUES(?, ?, ?, ?, ?)""",
                (
                    class_id,
                    "Buổi demo điểm danh Blockchain",
                    now_iso(),
                    "2099-12-31T23:59:59+00:00",
                    new_attendance_code(db),
                ),
            )


STUDENTS_PATH = Path(os.environ.get("ATTENDANCE_STUDENTS", ROOT / "students.json"))
CLASS_LIST_CODE = "CNTT2-K25"
CLASS_LIST_NAME = "Lớp CNTT2.K25"
CLASS_LIST_PASSWORD = "1"


def class_list_student_code(db: sqlite3.Connection, stt: int) -> str:
    """Mã SV dạng 2431482010xx theo số thứ tự trong danh sách lớp (tăng dần 10, 11, ...)."""
    base = "2431482010"
    suffix = str(10 + stt - 1)
    if len(suffix) > 2:
        return base + suffix
    code = base[: len(base) - len(suffix)] + suffix
    # Tránh trùng với tài khoản đã có (ví dụ sv01 = 243148201076)
    candidate = code
    offset = 0
    while db.execute("SELECT 1 FROM users WHERE username=?", (candidate,)).fetchone():
        offset += 1
        candidate = base[0:9] + str(30 + offset)
    return candidate


def seed_class_list(db: sqlite3.Connection) -> int:
    """Tạo tài khoản + ghi danh cho từng sinh viên trong students.json. Idempotent."""
    if not STUDENTS_PATH.exists():
        return 0
    try:
        students = json.loads(STUDENTS_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return 0
    db.execute(
        """INSERT OR IGNORE INTO classes(code, course_code, name, lecturer_username)
        VALUES(?, ?, ?, ?)""",
        (CLASS_LIST_CODE, "CNTT2", CLASS_LIST_NAME, "gv01"),
    )
    row = db.execute("SELECT id FROM classes WHERE code=?", (CLASS_LIST_CODE,)).fetchone()
    if not row:
        return 0
    list_class_id = row["id"]
    created = 0
    for student in students:
        name = str(student.get("name") or "").strip()
        stt = int(student.get("stt") or 0)
        if not name or not stt:
            continue
        alias = f"cntt2-{stt:02d}"
        # Tài khoản sẵn có cùng tên + lớp (như sv01 "Trần Gia Bảo") thì dùng lại, không tạo trùng.
        # Điều kiện list_class_code IS NULL để không lấy nhầm tài khoản của lần nạp trước.
        exists = db.execute(
            """SELECT username FROM users
            WHERE alias=? OR (name=? AND class_name=? AND list_class_code IS NULL) LIMIT 1""",
            (alias, name, "CNTT2.K25"),
        ).fetchone()
        if exists:
            username = exists["username"]
            # Đồng bộ tài khoản về đúng lớp danh sách: mật khẩu "1" để test nhanh
            db.execute(
                "UPDATE users SET alias=?, password=?, list_class_code=? WHERE username=?",
                (alias, CLASS_LIST_PASSWORD, CLASS_LIST_CODE, username),
            )
        else:
            username = class_list_student_code(db, stt)
            db.execute(
                """INSERT INTO users (
                    username, alias, password, role, name, student_id, class_name,
                    wallet_address, created_at, list_class_code
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    username,
                    alias,
                    CLASS_LIST_PASSWORD,
                    "student",
                    name,
                    username,
                    "CNTT2.K25",
                    generate_wallet(username),
                    now_iso(),
                    CLASS_LIST_CODE,
                ),
            )
            created += 1
        for key in (username, alias):
            db.execute(
                "INSERT OR IGNORE INTO enrollments(class_id, student_username) VALUES(?, ?)",
                (list_class_id, key),
            )
    return created


def find_user_in_db(db: sqlite3.Connection, identifier: str) -> dict[str, object] | None:
    row = db.execute(
        """SELECT * FROM users
        WHERE username = ? OR student_id = ? OR alias = ?""",
        (identifier, identifier, identifier),
    ).fetchone()
    return dict(row) if row else None


def find_login_user_in_db(
    db: sqlite3.Connection, identifier: str
) -> tuple[dict[str, object] | None, bool]:
    user = find_user_in_db(db, identifier)
    if user:
        return user, False

    normalized_name = " ".join(unicodedata.normalize("NFKC", identifier).casefold().split())
    matches = [
        dict(row)
        for row in db.execute("SELECT * FROM users").fetchall()
        if " ".join(unicodedata.normalize("NFKC", str(row["name"])).casefold().split())
        == normalized_name
    ]
    if len(matches) > 1:
        return None, True
    return (matches[0] if matches else None), False


def new_attendance_code(db: sqlite3.Connection) -> str:
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        if not db.execute("SELECT 1 FROM sessions WHERE attendance_code = ?", (code,)).fetchone():
            return code


def event_digest(event: dict[str, object]) -> str:
    canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_event(
    db: sqlite3.Connection, session_id: int, student_username: str, actor_username: str
) -> None:
    previous = db.execute("SELECT event_hash FROM attendance_events ORDER BY id DESC LIMIT 1").fetchone()
    previous_hash = previous["event_hash"] if previous else GENESIS_HASH
    occurred_at = now_iso()
    event = {
        "session_id": session_id,
        "student_username": student_username,
        "actor_username": actor_username,
        "event_type": "CHECK_IN",
        "occurred_at": occurred_at,
        "previous_hash": previous_hash,
    }
    db.execute(
        """
        INSERT INTO attendance_events(
            session_id, student_username, actor_username, event_type,
            occurred_at, previous_hash, event_hash
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            session_id,
            student_username,
            actor_username,
            "CHECK_IN",
            occurred_at,
            previous_hash,
            event_digest(event),
        ),
    )


def verify_chain(db: sqlite3.Connection) -> tuple[bool, int | None]:
    previous_hash = GENESIS_HASH
    rows = db.execute("SELECT * FROM attendance_events ORDER BY id").fetchall()
    for row in rows:
        event = {
            "session_id": row["session_id"],
            "student_username": row["student_username"],
            "actor_username": row["actor_username"],
            "event_type": row["event_type"],
            "occurred_at": row["occurred_at"],
            "previous_hash": row["previous_hash"],
        }
        if row["previous_hash"] != previous_hash or row["event_hash"] != event_digest(event):
            return False, row["id"]
        previous_hash = row["event_hash"]
    return True, None


def tamper_last_event(db: sqlite3.Connection) -> int | None:
    """Mô phỏng can thiệp: ghi đè hash của khối cuối. Trả về id khối đã sửa (None nếu chuỗi rỗng)."""
    row = db.execute("SELECT id, event_hash FROM attendance_events ORDER BY id DESC LIMIT 1").fetchone()
    if not row:
        return None
    event_id = row["id"]
    fake_hash = "deadbeef" + row["event_hash"][8:]
    db.execute("DROP TRIGGER IF EXISTS attendance_no_update")
    try:
        db.execute("UPDATE attendance_events SET event_hash = ? WHERE id = ?", (fake_hash, event_id))
    finally:
        db.execute(TRIGGER_NO_UPDATE_SQL)
    return int(event_id)


def inspect_chain(db: sqlite3.Connection) -> dict[str, object]:
    """Kiểm tra chuỗi (chỉ-đọc) và mô tả khối đầu tiên bị sai lệch."""
    valid, broken_at = verify_chain(db)
    details: list[dict[str, object]] = []
    if not valid:
        row = db.execute(
            """SELECT a.id, a.session_id, a.student_username, a.event_hash, s.title AS session_title
            FROM attendance_events a LEFT JOIN sessions s ON s.id = a.session_id
            WHERE a.id = ?""",
            (broken_at,),
        ).fetchone()
        if row:
            details.append({
                "block": row["id"],
                "session_id": row["session_id"],
                "session_title": row["session_title"] or "",
                "student_username": row["student_username"],
                "event_hash": row["event_hash"],
            })
    return {"chain_valid": valid, "broken_block": broken_at, "details": details}


def rebuild_chain(db: sqlite3.Connection) -> None:
    """Ghi lại previous_hash/event_hash cho toàn bộ chuỗi, luôn khôi phục ràng buộc chỉ-ghi."""
    db.execute("DROP TRIGGER IF EXISTS attendance_no_update")
    try:
        rows = db.execute("SELECT * FROM attendance_events ORDER BY id ASC").fetchall()
        previous_hash = GENESIS_HASH
        for r in rows:
            ev = {
                "session_id": r["session_id"],
                "student_username": r["student_username"],
                "actor_username": r["actor_username"],
                "event_type": r["event_type"],
                "occurred_at": r["occurred_at"],
                "previous_hash": previous_hash,
            }
            real_hash = event_digest(ev)
            db.execute(
                "UPDATE attendance_events SET previous_hash = ?, event_hash = ? WHERE id = ?",
                (previous_hash, real_hash, r["id"]),
            )
            previous_hash = real_hash
    finally:
        # Luôn khôi phục ràng buộc chỉ-ghi, kể cả khi ghi lại chuỗi thất bại
        db.execute(TRIGGER_NO_UPDATE_SQL)


def get_blockchain_ledger(db: sqlite3.Connection) -> dict[str, object]:
    rows = db.execute(
        """
        SELECT a.id, a.session_id, a.student_username, a.actor_username, a.event_type,
               a.occurred_at, a.previous_hash, a.event_hash,
               s.title AS session_title, c.code AS class_code, c.name AS class_name, c.course_code
        FROM attendance_events a
        JOIN sessions s ON s.id = a.session_id
        JOIN classes c ON c.id = s.class_id
        ORDER BY a.id ASC
        """
    ).fetchall()

    blocks: list[dict[str, object]] = []

    # Block 0: Genesis Block
    genesis_block = {
        "block_number": 0,
        "title": "Genesis Block (Khối Nguyên Thủy #0)",
        "timestamp": "2026-09-01T07:00:00+00:00",
        "previous_hash": "0" * 64,
        "block_hash": hashlib.sha256(b"GENESIS_BLOCK_STUDENT_ATTENDANCE_HPU_2026").hexdigest(),
        "merkle_root": hashlib.sha256(b"DEPLOY_SMART_CONTRACT_STUDENT_ATTENDANCE").hexdigest(),
        "nonce": 1024,
        "status": "VALID",
        "transactions": [
            {
                "tx_id": "0xgenesis000000000000000000000000000000000000000000000000000000000001",
                "type": "CONTRACT_INITIALIZATION",
                "sender": "0xF174a72d3e51A186dF52a7Eb51b8D14E69D89F9c (Admin Khoa CNTT)",
                "contract": "0x5FbDB2315678afecb367f032d93F642f64180aa3",
                "description": "Triển khai Smart Contract Điểm danh Sinh viên (Solidity 0.8.20)",
            }
        ],
    }
    blocks.append(genesis_block)

    valid, broken_id = verify_chain(db)
    current_previous = GENESIS_HASH

    for row in rows:
        event = {
            "session_id": row["session_id"],
            "student_username": row["student_username"],
            "actor_username": row["actor_username"],
            "event_type": row["event_type"],
            "occurred_at": row["occurred_at"],
            "previous_hash": row["previous_hash"],
        }
        calculated_hash = event_digest(event)
        is_block_valid = (
            row["previous_hash"] == current_previous
            and row["event_hash"] == calculated_hash
        )

        st_user = find_user_in_db(db, row["student_username"]) or {}
        student_display = st_user.get("name", row["student_username"])
        student_id_val = st_user.get("student_id", row["student_username"])
        class_name_val = st_user.get("class_name", "CNTT2.K25")
        student_wallet = st_user.get("wallet_address", generate_wallet(row["student_username"]))

        block = {
            "block_number": row["id"],
            "title": f"Block #{row['id']} - Proof-of-Attendance",
            "timestamp": row["occurred_at"],
            "previous_hash": row["previous_hash"],
            "block_hash": row["event_hash"],
            "calculated_hash": calculated_hash,
            "nonce": 42000 + row["id"],
            "status": "VALID" if is_block_valid else "CORRUPTED",
            "transactions": [
                {
                    "tx_id": "0x" + row["event_hash"][:40],
                    "type": "PROOF_OF_ATTENDANCE",
                    "student_username": row["student_username"],
                    "student_name": student_display,
                    "student_id": student_id_val,
                    "class_name": class_name_val,
                    "student_wallet": student_wallet,
                    "class_code": row["class_code"],
                    "course_name": row["class_name"],
                    "session_title": row["session_title"],
                    "occurred_at": row["occurred_at"],
                    "actor": row["actor_username"],
                    "signature": "ECDSA_SECP256K1_VERIFIED",
                }
            ],
        }
        blocks.append(block)
        current_previous = row["event_hash"]

    return {
        "valid": valid,
        "broken_at": broken_id,
        "total_blocks": len(blocks),
        "total_attendance_events": len(rows),
        "latest_hash": current_previous,
        "consensus": "Proof-of-Authority (PoA) / SHA-256 Hash Chain",
        "blocks": blocks,
    }


def make_qr_token(session_id: int) -> str:
    payload = json.dumps(
        {"session_id": session_id, "expires": int(time.time()) + TOKEN_LIFETIME},
        separators=(",", ":"),
    ).encode("utf-8")
    encoded = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(get_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{encoded}.{signature}"


def parse_qr_token(token: str) -> int:
    try:
        encoded, signature = token.split(".", 1)
        expected = hmac.new(get_secret(), encoded.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(signature, expected):
            raise ValueError("Mã QR không hợp lệ.")
        payload = json.loads(
            base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4)).decode("utf-8")
        )
        if int(payload["expires"]) < int(time.time()):
            raise ValueError("Mã QR đã hết hạn.")
        return int(payload["session_id"])
    except (
        ValueError,
        KeyError,
        TypeError,
        json.JSONDecodeError,
        UnicodeDecodeError,
        binascii.Error,
    ):
        raise ValueError("Mã QR không hợp lệ hoặc đã hết hạn.") from None


def decode_attendance_qr_image(data_url: str) -> str:
    header, separator, encoded = data_url.partition(",")
    if not separator or not header.lower().startswith("data:image/"):
        raise ValueError("Tệp tải lên không phải ảnh hợp lệ.")
    try:
        image_bytes = base64.b64decode(encoded, validate=True)
    except (ValueError, binascii.Error):
        raise ValueError("Dữ liệu ảnh không hợp lệ.") from None
    if not image_bytes or len(image_bytes) > QR_IMAGE_MAX_BYTES:
        raise ValueError("Ảnh QR phải nhỏ hơn 4 MB.")

    try:
        with Image.open(io.BytesIO(image_bytes)) as uploaded:
            if uploaded.width * uploaded.height > QR_IMAGE_MAX_PIXELS:
                raise ValueError("Ảnh QR có kích thước quá lớn.")
            if uploaded.format not in ("PNG", "JPEG", "WEBP", "BMP", "GIF"):
                raise ValueError("Chỉ hỗ trợ ảnh PNG, JPEG, WEBP, BMP hoặc GIF.")
            results = zxingcpp.read_barcodes(
                uploaded, formats=zxingcpp.BarcodeFormat.QRCode
            )
    except (UnidentifiedImageError, OSError, Image.DecompressionBombError):
        raise ValueError("Không thể mở tệp ảnh.") from None

    if not results:
        raise ValueError("Không tìm thấy mã QR trong ảnh.")

    value = results[0].text
    parsed = urlparse(value)
    codes = parse_qs(parsed.query).get("code")
    if codes:
        return codes[0]
    if parsed.scheme or parsed.netloc:
        raise ValueError("QR trong ảnh không phải đường dẫn điểm danh.")
    return value


def qr_svg(value: str) -> bytes:
    image = qrcode.make(
        value,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=8,
        border=4,
    )
    output = io.BytesIO()
    image.save(output)
    return output.getvalue()


HTML = r"""<!doctype html>
<html lang="vi">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>Hệ thống Điểm danh Sinh viên bằng Blockchain - FIT HPU</title>
  <style>
    :root {
      --primary: #0f4c81;
      --primary-dark: #0b3459;
      --primary-light: #e6f0fa;
      --accent: #00a896;
      --accent-hover: #028090;
      --success: #16a34a;
      --warning: #f59e0b;
      --danger: #dc2626;
      --bg: #f8fafc;
      --card-bg: #ffffff;
      --text: #1e293b;
      --muted: #64748b;
      --border: #e2e8f0;
      --radius: 12px;
      --shadow: 0 4px 14px 0 rgba(15, 23, 42, 0.08);
    }
    * { box-sizing: border-box; margin: 0; padding: 0; }
    body {
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      background: var(--bg);
      color: var(--text);
      line-height: 1.5;
      padding-bottom: 60px;
    }
    header {
      background: linear-gradient(135deg, var(--primary-dark) 0%, var(--primary) 100%);
      color: white;
      padding: 22px 20px;
      box-shadow: 0 4px 20px rgba(0,0,0,0.15);
    }
    .header-container {
      max-width: 1200px;
      margin: 0 auto;
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      gap: 16px;
    }
    .brand { display: flex; align-items: center; gap: 16px; }
    .brand-icon {
      font-size: 38px;
      background: rgba(255,255,255,0.15);
      border-radius: 12px;
      padding: 6px 14px;
      display: flex;
      align-items: center;
      justify-content: center;
    }
    .brand h1 { font-size: 22px; font-weight: 700; letter-spacing: -0.5px; }
    .brand p { font-size: 13px; opacity: 0.9; }
    .badge {
      display: inline-block;
      padding: 4px 10px;
      border-radius: 9999px;
      font-size: 12px;
      font-weight: 600;
    }
    .badge-lecturer { background: #dbeafe; color: #1e40af; }
    .badge-student { background: #dcfce7; color: #166534; }
    .badge-valid { background: #dcfce7; color: #15803d; }
    .badge-tamper { background: #fee2e2; color: #b91c1c; }

    .user-profile {
      background: rgba(255,255,255,0.14);
      padding: 10px 16px;
      border-radius: 10px;
      text-align: right;
      font-size: 13px;
    }
    .wallet-pill {
      font-family: monospace;
      font-size: 11px;
      background: rgba(0,0,0,0.25);
      padding: 3px 8px;
      border-radius: 6px;
      margin-top: 4px;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }

    .container {
      max-width: 1200px;
      margin: 24px auto;
      padding: 0 16px;
    }

    /* Tabs */
    .tabs {
      display: flex;
      flex-wrap: wrap;
      gap: 8px;
      margin-bottom: 20px;
      border-bottom: 2px solid var(--border);
      padding-bottom: 8px;
    }
    .tab-btn {
      background: none;
      border: none;
      font-size: 15px;
      font-weight: 600;
      color: var(--muted);
      padding: 10px 18px;
      border-radius: 8px;
      cursor: pointer;
      display: flex;
      align-items: center;
      gap: 8px;
      transition: all 0.2s;
    }
    .tab-btn:hover { background: var(--primary-light); color: var(--primary); }
    .tab-btn.active { background: var(--primary); color: white; }

    /* Cards */
    .card {
      background: var(--card-bg);
      border-radius: var(--radius);
      padding: 24px;
      margin-bottom: 24px;
      box-shadow: var(--shadow);
      border: 1px solid var(--border);
    }
    .card h2 {
      font-size: 18px;
      color: var(--primary-dark);
      margin-bottom: 16px;
      display: flex;
      align-items: center;
      gap: 10px;
    }
    .card h3 {
      font-size: 15px;
      margin: 16px 0 10px 0;
      color: var(--text);
    }

    .row { display: flex; flex-wrap: wrap; gap: 12px; align-items: center; margin: 8px 0; }
    input, select, button {
      font-size: 14px;
      padding: 10px 14px;
      border: 1px solid var(--border);
      border-radius: 8px;
      outline: none;
    }
    input:focus, select:focus { border-color: var(--primary); }
    button {
      background: var(--primary);
      color: white;
      font-weight: 600;
      cursor: pointer;
      border: none;
      transition: background 0.2s;
      display: inline-flex;
      align-items: center;
      gap: 6px;
    }
    button:hover { background: var(--primary-dark); }
    button.btn-accent { background: var(--accent); }
    button.btn-accent:hover { background: var(--accent-hover); }
    button.btn-danger { background: var(--danger); }
    button.btn-danger:hover { background: #b91c1c; }
    button.btn-outline { background: white; color: var(--text); border: 1px solid var(--border); }
    button.btn-outline:hover { background: #f1f5f9; }
    button.btn-sm { padding: 6px 12px; font-size: 12px; border-radius: 6px; }

    /* Tables */
    .table-wrap { overflow-x: auto; margin-top: 14px; border-radius: 8px; border: 1px solid var(--border); }
    table { width: 100%; border-collapse: collapse; background: white; font-size: 14px; }
    th, td { padding: 12px 14px; text-align: left; border-bottom: 1px solid var(--border); }
    th { background: #f1f5f9; font-weight: 600; color: var(--muted); }
    tr:last-child td { border-bottom: none; }
    tr:hover td { background: #f8fafc; }
    .status-present { color: var(--success); font-weight: 700; }
    .status-absent { color: var(--danger); font-weight: 700; }
    .status-pending { color: #f59e0b; font-weight: 700; }
    .status-open { color: #0284c7; font-weight: 700; }
    .empty { color: var(--muted); text-align: center; padding: 24px; }

    /* Modal */
    .modal-overlay {
      position: fixed;
      inset: 0;
      background: rgba(0,0,0,0.75);
      display: flex;
      justify-content: center;
      align-items: center;
      z-index: 9999;
      padding: 16px;
    }
    .modal-content {
      background: white;
      border-radius: 16px;
      padding: 28px;
      max-width: 900px;
      width: 100%;
      max-height: 90vh;
      overflow-y: auto;
      box-shadow: 0 20px 40px rgba(0,0,0,0.3);
    }
    .modal-qr-img { width: 260px; height: 260px; margin: 16px auto; }
    .pin-display {
      font-size: 40px;
      font-weight: 900;
      letter-spacing: 6px;
      color: var(--primary);
      background: var(--primary-light);
      padding: 10px 24px;
      border-radius: 12px;
      display: inline-block;
      margin: 12px 0;
      font-family: monospace;
    }

    #msg {
      background: #f1f5f9;
      border-left: 4px solid var(--primary);
      padding: 12px 16px;
      border-radius: 6px;
      font-family: monospace;
      font-size: 13px;
      margin-top: 14px;
      white-space: pre-wrap;
    }
    .hidden { display: none !important; }

    /* Blockchain Blocks */
    .block-card {
      background: #ffffff;
      border-radius: 12px;
      border: 2px solid #cbd5e1;
      padding: 18px;
      box-shadow: 0 4px 12px rgba(0,0,0,0.05);
      margin-bottom: 12px;
    }
    .block-card.valid { border-color: var(--success); }
    .block-card.tampered { border-color: var(--danger); background: #fff5f5; }
    .block-hash-box {
      background: #f8fafc;
      font-family: monospace;
      font-size: 12px;
      padding: 6px 10px;
      border-radius: 6px;
      margin: 4px 0;
      word-break: break-all;
      border: 1px solid #e2e8f0;
    }
    .tx-item {
      background: #f1f5f9;
      border-left: 3px solid var(--accent);
      padding: 8px 12px;
      border-radius: 4px;
      margin-top: 8px;
      font-size: 13px;
    }
    .grid-stats {
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
      gap: 16px;
      margin-bottom: 20px;
    }
    .stat-card {
      background: white;
      border: 1px solid var(--border);
      padding: 16px;
      border-radius: 10px;
      box-shadow: var(--shadow);
    }
    .stat-card p { font-size: 13px; color: var(--muted); }
    .stat-card h3 { font-size: 22px; color: var(--primary-dark); font-weight: 700; }
  </style>
</head>
<body>

<header>
  <div class="header-container">
    <div class="brand">
      <div class="brand-icon">⛓️</div>
      <div>
        <p style="font-size: 11px; text-transform: uppercase; letter-spacing: 1px; color: #a5c3e7;">
          Trường Đại Học Hải Phòng · Khoa Công Nghệ Thông Tin
        </p>
        <h1>Hệ Thống Điểm Danh Sinh Viên Bằng Blockchain</h1>
        <p>Proof-of-Attendance · Cấp Tài Khoản Theo Mã Sinh Viên (MSV) · Lưu Trữ Database & Chuỗi Khối</p>
      </div>
    </div>
    <div id="userHeader" class="user-profile hidden">
      <span id="userName" style="font-weight: bold;"></span>
      <span id="userBadge" class="badge"></span>
      <div id="studentSubInfo" style="font-size:12px; margin-top:2px; color:#e0f2fe;"></div>
      <div class="wallet-pill" title="Địa chỉ ví xác thực trên Blockchain">
        <span>🔑 Ví:</span> <span id="userWallet"></span>
      </div>
      <button class="btn-outline btn-sm" onclick="logout()">Đăng xuất</button>
    </div>
  </div>
</header>

<div class="container">
  <!-- AUTH CARD -->
  <section class="card" id="authSection">
    <h2>🔐 1. Đăng Nhập Hệ Thống</h2>
    <div class="row">
      <input id="username" placeholder="Họ tên đầy đủ hoặc tài khoản giảng viên" style="min-width: 200px;">
      <input id="password" placeholder="Mật khẩu" type="password" style="min-width: 140px;">
      <button onclick="login()">⚡ Đăng nhập</button>
    </div>
    <p id="authMessage" role="alert" style="color:var(--danger);"></p>
    <div style="margin-top: 14px; font-size: 13px; color: var(--muted); line-height: 1.8;">
      💡 Sinh viên đăng nhập bằng <b>họ tên đầy đủ</b> và mật khẩu được cấp (tài khoản danh sách lớp dùng mật khẩu <code>1</code>).<br>
      Giảng viên tiếp tục đăng nhập bằng tài khoản <code>gv01</code>.
    </div>
  </section>

  <div id="dashboard" class="hidden">
  <!-- NAVIGATION TABS -->
  <div class="tabs" id="mainTabs" style="display: none;">
    <button class="tab-btn active" onclick="switchTab('attendanceTab', this)">📋 Quản Lý & Điểm Danh</button>
    <button class="tab-btn" onclick="switchTab('blockchainTab', this)">⛓️ Blockchain Explorer & Sổ Cái</button>
    <button class="tab-btn" onclick="switchTab('contractTab', this)">📜 Smart Contract (Solidity)</button>
  </div>

  <!-- TAB 1: ATTENDANCE & MANAGEMENT -->
  <div id="attendanceTab">
    <!-- LECTURER PANEL -->
    <section class="card" id="lecturerPanel" hidden>
      <h2>👨‍🏫 Bảng Điều Khiển Giảng Viên</h2>

      <!-- THÊM SINH VIÊN MỚI VÀO DATABASE THEO MSV -->
      <div style="background:#f8fafc; border:1px solid #cbd5e1; border-radius:10px; padding:18px; margin-bottom:24px;">
        <h3 style="margin-top:0; color:var(--primary-dark);">🎓 Cấp Tài Khoản Sinh Viên Mới Theo MSV (Lưu Database)</h3>
        <p style="font-size:13px; color:var(--muted); margin-bottom:12px;">
          Tài khoản sinh viên sẽ được tạo với <b>Username = Mã Sinh Viên (MSV)</b> và tự động sinh địa chỉ ví Blockchain.
        </p>
        <div class="row">
          <input id="newStudentId" placeholder="Mã Sinh Viên (VD: 243148201099)" style="min-width:180px;">
          <input id="newStudentName" placeholder="Họ và Tên (VD: Nguyễn Văn A)" style="min-width:200px;">
          <input id="newStudentClass" placeholder="Lớp (VD: CNTT2.K25)" style="min-width:130px;" value="CNTT2.K25">
          <input id="newStudentPass" placeholder="Mật khẩu (mặc định SVdemo123!)" style="min-width:160px;" value="SVdemo123!">
          <button class="btn-accent" onclick="createNewStudent()">➕ Cấp Tài Khoản Theo MSV</button>
        </div>
      </div>
      
      <h3>1. Tạo Môn học / Lớp học mới</h3>
      <div class="row">
        <input id="courseCode" placeholder="Mã môn (VD: BLK101)">
        <input id="classCode" placeholder="Mã lớp (VD: CNTT2-K25)">
        <input id="className" placeholder="Tên môn học (VD: Lập trình Blockchain)" style="flex: 1; min-width: 220px;">
        <button class="btn-accent" onclick="createClass()">+ Tạo lớp học</button>
      </div>

      <h3 style="margin-top: 24px;">2. Chọn Lớp & Ghi danh sinh viên theo MSV</h3>
      <div class="row">
        <select id="classes" onchange="showSelectedClass()" style="min-width: 240px;"></select>
        <select id="availableStudentsSelect" style="min-width: 240px;"></select>
        <input id="studentUsername" placeholder="Hoặc gõ trực tiếp MSV..." style="width: 170px;">
        <button onclick="enrollStudent()">+ Ghi danh vào lớp</button>
      </div>
      <div id="selectedClass" style="font-size: 13px; color: var(--muted); margin: 4px 0 16px 0;"></div>

      <h3 style="margin-top: 24px;">3. Tạo Buổi học & Phát hành mã điểm danh QR</h3>
      <div class="row">
        <input id="sessionTitle" placeholder="Tiêu đề buổi (VD: Buổi 1: Giới thiệu Blockchain)" style="flex: 1; min-width: 250px;">
        <button class="btn-accent" onclick="createSession()">🚀 Mở buổi & Tạo mã QR</button>
        <input id="sessionId" placeholder="Mã ID buổi cần đóng" style="width: 140px;">
        <button class="btn-danger" onclick="closeSession()">🔒 Đóng buổi (Khóa sổ)</button>
        <button class="btn-outline" onclick="loadSessions()">🔄 Làm mới</button>
      </div>

      <!-- Quick Session Live Info Box -->
      <div id="liveSessionCard" class="card" style="margin-top: 16px; background: var(--primary-light); border: 1px dashed var(--primary); display: none;">
        <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px;">
          <div>
            <h3 id="liveSessionTitle" style="margin: 0; color: var(--primary-dark);"></h3>
            <p style="font-size: 14px; margin-top: 4px;">Mã PIN điểm danh trực tiếp: <span id="attendanceCodeDisplay" style="font-weight: bold; font-size: 20px; color: var(--danger); font-family: monospace;"></span></p>
          </div>
          <div style="display: flex; gap: 8px;">
            <button class="btn-accent" onclick="openProjectorModal()">📺 Phóng to QR Chiếu Máy Chiếu</button>
          </div>
        </div>
        <div class="row" style="margin-top: 12px;">
          <img id="qr" style="width: 130px; height: 130px; background: white; padding: 6px; border-radius: 8px; border: 1px solid #ccc;">
          <div style="flex: 1; font-size: 13px;">
            <p><b>🔗 Link điểm danh trực tiếp:</b></p>
            <p id="qrLink" style="word-break: break-all; color: var(--primary); font-family: monospace;"></p>
            <p style="color: var(--muted); margin-top: 6px;">Mã QR có chữ ký số HMAC-SHA256, tự động vô hiệu hóa khi hết hạn hoặc khi giảng viên đóng buổi.</p>
          </div>
        </div>
      </div>
    </section>

    <!-- STUDENT PANEL -->
    <section class="card" id="studentPanel" hidden>
      <h2>🎓 Cổng Điểm Danh Sinh Viên</h2>
      <div id="studentIdentityCard" style="background:#e0f2fe; padding:14px; border-radius:8px; margin-bottom:16px;">
        <p><b>Họ và tên:</b> <span id="stName"></span> | <b>Mã Sinh Viên (Tài khoản):</b> <span id="stId"></span> | <b>Lớp:</b> <span id="stClass"></span></p>
      </div>

      <p style="margin-bottom: 12px; font-size: 14px; color: var(--muted);">
        Quét mã QR từ màn hình của giảng viên hoặc nhập mã PIN 6 số để ký giao dịch điểm danh:
      </p>

      <div class="row">
        <input id="qrToken" placeholder="Dán mã QR Token hoặc nhập Mã PIN 6 số..." style="flex: 1; min-width: 280px; font-family: monospace;">
        <button id="qrScanButton" class="btn-outline" onclick="startQrScanner()">📷 Quét QR bằng camera</button>
        <button class="btn-outline" onclick="document.querySelector('#qrImageInput').click()">🖼️ Chọn ảnh QR</button>
        <input id="qrImageInput" type="file" accept="image/png,image/jpeg,image/webp,image/bmp,image/gif" hidden onchange="scanQrImage(event)">
        <button class="btn-accent" onclick="checkIn()">✍️ Xác nhận Điểm danh</button>
      </div>
      <p style="margin-top: 6px; font-size: 12px; color: var(--muted);">Không cấp quyền camera? Chọn ảnh QR đã lưu (PNG, JPEG, WEBP, BMP hoặc GIF; tối đa 4 MB).</p>
      <div id="qrScanner" class="hidden" style="margin-top: 12px;">
        <video id="qrVideo" autoplay playsinline style="display:block; width:100%; max-width:480px; max-height:360px; background:#0f172a; border-radius:8px;"></video>
        <p id="qrScannerMessage" role="status" style="margin:8px 0;"></p>
        <button class="btn-outline btn-sm" onclick="stopQrScanner()">Đóng camera</button>
      </div>
    </section>

    <!-- SESSIONS LIST & ATTENDEES VIEW -->
    <section class="card" id="classPanel" hidden>
      <h2>📊 Thống Kê Các Môn Học & Điểm Danh</h2>
      <div id="classList"></div>

      <div style="display: flex; justify-content: space-between; align-items: center; margin: 24px 0 12px 0;">
        <h3 style="margin: 0;">Danh Sách Chi Tiết Các Buổi Học</h3>
        <button class="btn-outline" onclick="loadSessions()">🔄 Tải lại dữ liệu</button>
      </div>
      <div id="sessions"></div>
    </section>
  </div>

  <!-- TAB 2: BLOCKCHAIN EXPLORER -->
  <div id="blockchainTab" class="hidden">
    <div class="grid-stats">
      <div class="stat-card">
        <p>Tổng Số Khối (Blocks)</p>
        <h3 id="statTotalBlocks">0</h3>
      </div>
      <div class="stat-card">
        <p>Lượt Điểm Danh Đã Ký On-Chain</p>
        <h3 id="statTotalTxs">0</h3>
      </div>
      <div class="stat-card">
        <p>Trạng Thái Toàn Vẹn Chuỗi</p>
        <h3 id="statChainStatus" style="color: var(--success);">HỢP LỆ ✓</h3>
      </div>
      <div class="stat-card">
        <p>Cơ Chế Băm Mật Mã</p>
        <h3 style="font-size: 18px;">SHA-256 Hash Chain</h3>
      </div>
    </div>

    <section class="card">
      <div style="display: flex; justify-content: space-between; align-items: center; flex-wrap: wrap; gap: 12px; margin-bottom: 16px;">
        <div>
          <h2>⛓️ Sổ Cái Chuỗi Khối (Visual Blockchain Ledger)</h2>
          <p style="font-size: 13px; color: var(--muted);">
            Chỉ cần một sự kiện điểm danh lệch đi 1 bit là toàn bộ khối phía sau (theo chữ ký số và chuỗi băm) bị đánh dấu hỏng. Khi sửa
            lại dữ liệu gốc, chữ ký số và liên kết chuỗi được tạo lại từ dữ liệu đã khôi phục.
          </p>
        </div>
        <div style="display: flex; gap: 8px; flex-wrap: wrap;">
          <button class="btn-accent" onclick="loadBlockchainExplorer()">🔍 Kiểm Chứng Chuỗi Khối</button>
          <button class="btn-danger" onclick="simulateTamper()">⚡ Thử Nghiệm: Giả Lập Sửa Dữ Liệu (chỉ trong demo)</button>
          <button class="btn-outline" onclick="repairBlockchain()">🛡️ Khôi Phục Dữ Liệu Gốc</button>
        </div>
      </div>

      <div id="tamperAlertBox"></div>
      <div id="blockchainBlocks"></div>
    </section>
  </div>

  <!-- TAB 3: SMART CONTRACT SOLIDITY -->
  <div id="contractTab" class="hidden">
    <section class="card">
      <h2>📜 Hợp Đồng Thông Minh: StudentAttendance.sol</h2>
      <p style="color: var(--muted); font-size: 13px; margin-bottom: 14px;">
        Mã nguồn Solidity 0.8.20 cho Ethereum EVM với cơ chế RBAC, băm chứng thực on-chain và chống Replay attack.
      </p>

      <div style="background: #1e293b; color: #f8fafc; padding: 18px; border-radius: 8px; font-family: monospace; font-size: 13px; overflow-x: auto; max-height: 520px;">
<pre>// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

contract StudentAttendance {
    struct SessionInfo {
        uint256 sessionId;
        string classCode;
        string title;
        uint256 startTime;
        uint256 endTime;
        bytes32 sessionSecretHash;
        uint8 status; // 0: Closed, 1: Open
        uint256 totalPresent;
    }

    mapping(string => ClassInfo) public classes;
    mapping(uint256 => SessionInfo) public sessions;
    mapping(uint256 => mapping(address => bool)) public isPresent;
    mapping(address => string) public studentNames;
    mapping(address => string) public studentIds;
    bytes32 public latestGlobalHash;

    event AttendanceCheckedIn(
        uint256 indexed sessionId,
        address indexed student,
        string studentId,
        string studentName,
        uint256 timestamp,
        bytes32 verificationHash
    );

    function checkIn(uint256 _sessionId, string calldata _secret) external {
        SessionInfo storage session = sessions[_sessionId];
        require(session.status == 1, "Buoi hoc da dong");
        require(block.timestamp <= session.endTime, "Ma QR da het han");
        require(keccak256(abi.encodePacked(_secret)) == session.sessionSecretHash, "Sai ma xac thuc");
        require(!isPresent[_sessionId][msg.sender], "Da diem danh roi");

        isPresent[_sessionId][msg.sender] = true;
        session.totalPresent += 1;

        bytes32 vHash = keccak256(abi.encodePacked(_sessionId, msg.sender, block.timestamp, latestGlobalHash));
        latestGlobalHash = vHash;

        emit AttendanceCheckedIn(_sessionId, msg.sender, studentIds[msg.sender], studentNames[msg.sender], block.timestamp, vHash);
    }
}</pre>
      </div>
    </section>
  </div>

  <!-- LOG MESSAGE OUTPUT -->
  <pre id="msg"></pre>
  </div>
</div>

<!-- MODAL: DANH SÁCH SINH VIÊN ĐIỂM DANH TRONG BUỔI HỌC (CHO GIẢNG VIÊN) -->
<div id="attendeesModal" class="modal-overlay hidden">
  <div class="modal-content">
    <div style="display:flex; justify-content:space-between; align-items:center; border-bottom:1px solid #e2e8f0; padding-bottom:12px; margin-bottom:16px;">
      <div>
        <h2 id="modalSessionTitle" style="color:var(--primary); font-size:20px; margin:0;">Danh Sách Sinh Viên Buổi Học</h2>
        <p id="modalSessionSub" style="font-size:13px; color:var(--muted); margin-top:4px;"></p>
      </div>
      <button class="btn-outline" onclick="closeAttendeesModal()">✕ Đóng</button>
    </div>

    <div style="display:flex; gap:16px; margin-bottom:16px;">
      <div style="background:#f1f5f9; padding:10px 16px; border-radius:8px;">
        Sĩ số: <b id="statRosterTotal">0</b> sinh viên
      </div>
      <div style="background:#dcfce7; color:#15803d; padding:10px 16px; border-radius:8px;">
        Đã có mặt: <b id="statRosterPresent">0</b> sinh viên
      </div>
      <div style="background:#fee2e2; color:#b91c1c; padding:10px 16px; border-radius:8px;">
        Vắng: <b id="statRosterAbsent">0</b> sinh viên
      </div>
    </div>

    <div id="attendeesTableBox"></div>
  </div>
</div>

<!-- PROJECTOR QR MODAL -->
<div id="projectorModal" class="modal-overlay hidden">
  <div class="modal-content" style="max-width:560px; text-align:center;">
    <h2 id="modalTitle" style="color: var(--primary); font-size: 24px;">MÃ ĐIỂM DANH BUỔI HỌC</h2>
    <p style="color: var(--muted); font-size: 14px;">Sinh viên quét mã QR hoặc nhập mã PIN bên dưới để điểm danh</p>
    <div class="pin-display" id="modalPin">000000</div>
    <div>
      <img id="modalQrImg" class="modal-qr-img" src="" alt="Mã QR Điểm Danh">
    </div>
    <p style="font-size: 13px; color: var(--muted); margin-bottom: 20px;">
      ⏳ Mã QR và mã PIN được bảo vệ bằng chữ ký mật mã Blockchain (HMAC-SHA256).
    </p>
    <button class="btn-danger" style="padding: 10px 28px;" onclick="closeProjectorModal()">Đóng cửa sổ</button>
  </div>
</div>

<script>
let auth = localStorage.getItem('attendance-token') || '';
let currentUser = null;
let currentViewingSessionId = null;
let qrCameraStream = null;
let qrScanFrame = 0;
let sessionsRefreshTimer = null;
const urlCode = new URLSearchParams(location.search).get('code');
if (urlCode) document.querySelector('#qrToken').value = urlCode;

function show(x) {
  const el = document.querySelector('#msg');
  el.textContent = typeof x === 'string' ? x : JSON.stringify(x, null, 2);
}

async function api(path, method = 'GET', body) {
  let r = await fetch(path, {
    method,
    headers: {
      'Content-Type': 'application/json',
      ...(auth ? { Authorization: 'Bearer ' + auth } : {})
    },
    ...(body ? { body: JSON.stringify(body) } : {})
  });
  let d = await r.json();
  if (!r.ok) throw Error(d.error || r.statusText);
  return d;
}

function switchTab(tabId, button) {
  if (!currentUser || (currentUser.role !== 'lecturer' && tabId !== 'attendanceTab')) return;
  ['attendanceTab', 'blockchainTab', 'contractTab'].forEach(id => {
    document.querySelector('#' + id).classList.add('hidden');
  });
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));
  document.querySelector('#' + tabId).classList.remove('hidden');
  if (button) button.classList.add('active');

  if (tabId === 'blockchainTab') {
    loadBlockchainExplorer();
  }
}

function setRole(user) {
  currentUser = user;
  if (sessionsRefreshTimer) clearInterval(sessionsRefreshTimer);
  sessionsRefreshTimer = setInterval(() => {
    if (
      currentUser &&
      !document.querySelector('#dashboard').classList.contains('hidden')
    ) {
      loadSessions();
      if (
        currentViewingSessionId &&
        !document.querySelector('#attendeesModal').classList.contains('hidden')
      ) {
        refreshAttendeesList();
      }
    }
  }, 5000);
  document.querySelector('#authSection').classList.add('hidden');
  document.querySelector('#dashboard').classList.remove('hidden');
  const userHeader = document.querySelector('#userHeader');
  userHeader.classList.remove('hidden');
  document.querySelector('#userName').textContent = user.name;
  document.querySelector('#userWallet').textContent = user.wallet || '0x...';
  document.querySelector('#attendanceTab').classList.remove('hidden');
  document.querySelector('#blockchainTab').classList.add('hidden');
  document.querySelector('#contractTab').classList.add('hidden');
  document.querySelectorAll('.tab-btn').forEach(btn => btn.classList.remove('active'));

  const badge = document.querySelector('#userBadge');
  const subInfo = document.querySelector('#studentSubInfo');

  if (user.role === 'lecturer') {
    badge.textContent = 'Giảng viên';
    badge.className = 'badge badge-lecturer';
    subInfo.textContent = 'Khoa Công Nghệ Thông Tin - ĐH Hải Phòng';
    document.querySelector('#lecturerPanel').hidden = false;
    document.querySelector('#studentPanel').hidden = true;
    document.querySelector('#mainTabs').style.display = 'flex';
    document.querySelector('#mainTabs .tab-btn').classList.add('active');
    loadRegisteredStudents();
  } else {
    badge.textContent = 'Sinh viên';
    badge.className = 'badge badge-student';
    subInfo.textContent = `MSV: ${user.student_id || user.username} · Lớp: ${user.class_name || '—'}`;
    document.querySelector('#lecturerPanel').hidden = true;
    document.querySelector('#studentPanel').hidden = false;

    document.querySelector('#stName').textContent = user.name;
    document.querySelector('#stId').textContent = user.student_id || user.username;
    document.querySelector('#stClass').textContent = user.class_name || '—';
    document.querySelector('#mainTabs').style.display = 'none';
  }
  document.querySelector('#classPanel').hidden = false;
}

function renderTable(target, headers, rows, emptyMessage) {
  const box = document.querySelector(target);
  box.replaceChildren();
  if (!rows.length) {
    const p = document.createElement('p');
    p.className = 'empty';
    p.textContent = emptyMessage;
    box.append(p);
    return;
  }
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  const table = document.createElement('table');
  const thead = document.createElement('thead');
  const headerRow = document.createElement('tr');
  headers.forEach(text => {
    const th = document.createElement('th');
    th.textContent = text;
    headerRow.append(th);
  });
  thead.append(headerRow);
  const tbody = document.createElement('tbody');
  rows.forEach(row => {
    const tr = document.createElement('tr');
    row.forEach(cell => {
      const td = document.createElement('td');
      if (cell && typeof cell === 'object') {
        if (cell.html) {
          td.innerHTML = cell.html;
        } else {
          td.textContent = cell.text;
          td.className = cell.className || '';
        }
      } else {
        td.textContent = cell ?? '—';
      }
      tr.append(td);
    });
    tbody.append(tr);
  });
  table.append(thead, tbody);
  wrap.append(table);
  box.append(wrap);
}

function attendanceLabel(status) {
  if (status === 'present') return { text: '✓ Có mặt', className: 'status-present' };
  if (status === 'absent') return { text: '✗ Vắng', className: 'status-absent' };
  return { text: '⏳ Chưa điểm danh', className: 'status-pending' };
}

function renderSessions(classes, rows) {
  const summary = classes.map(c => {
    const meetings = rows.filter(s => s.class_id === c.id);
    if (currentUser && currentUser.role === 'lecturer') {
      const attended = meetings.reduce((n, s) => n + s.attended_count, 0);
      const absent = meetings.filter(s => s.status === 'closed').reduce((n, s) => n + s.enrolled_count - s.attended_count, 0);
      const open = meetings.filter(s => s.status === 'open').length;
      return [c.course_code || '—', c.code, c.name, meetings.length, attended, absent, open];
    }
    const attended = meetings.filter(s => s.attendance_status === 'present').length;
    const absent = meetings.filter(s => s.attendance_status === 'absent').length;
    const pending = meetings.filter(s => s.attendance_status === 'pending').length;
    const total = meetings.length;
    const rate = total > 0 ? Math.round((attended / total) * 100) + '%' : '100%';
    return [c.course_code || '—', c.code, c.name, total, attended, absent, rate];
  });

  const summaryHeaders = (currentUser && currentUser.role === 'lecturer')
    ? ['Mã môn', 'Mã lớp', 'Tên học phần', 'Số buổi', 'Tổng lượt có mặt', 'Lượt vắng', 'Buổi đang mở']
    : ['Mã môn', 'Mã lớp', 'Tên học phần', 'Số buổi', 'Đã tham gia', 'Vắng', 'Tỷ lệ chuyên cần'];

  renderTable('#classList', summaryHeaders, summary, 'Chưa có môn học nào được ghi nhận.');

  if (currentUser && currentUser.role === 'lecturer') {
    renderTable('#sessions', ['ID', 'Học phần', 'Buổi học', 'Mã PIN', 'Trạng thái', 'Điểm danh', 'Danh Sách Sinh Viên'], rows.map(s => [
      s.id,
      s.class_name,
      s.title,
      s.attendance_code,
      s.status === 'closed' ? 'Đã đóng' : { text: 'Đang mở', className: 'status-open' },
      `${s.attended_count}/${s.enrolled_count} SV`,
      { html: `<button class="btn-accent btn-sm" onclick="viewSessionAttendees(${s.id}, '${escapeHtml(s.title)}')">👥 Xem Sinh Viên</button>` }
    ]), 'Chưa có buổi học nào.');
  } else {
    renderTable('#sessions', ['Mã môn', 'Học phần', 'Buổi học', 'Trạng thái buổi', 'Điểm danh của bạn'], rows.map(s => [
      s.course_code || s.class_code,
      s.class_name,
      s.title,
      s.status === 'closed' ? 'Đã đóng' : { text: 'Đang mở', className: 'status-open' },
      attendanceLabel(s.attendance_status)
    ]), 'Chưa có buổi học nào trong các môn bạn đăng ký.');
  }
}

function escapeHtml(text) {
  return String(text).replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

// TẢI DANH SÁCH SINH VIÊN ĐÃ CẤP TÀI KHOẢN TRONG DATABASE
async function loadRegisteredStudents() {
  try {
    let d = await api('/api/students');
    const select = document.querySelector('#availableStudentsSelect');
    if (select) {
      select.replaceChildren(
        ...d.students.map(st => {
          let o = document.createElement('option');
          o.value = st.student_id;
          o.textContent = `${st.student_id} — ${st.name} (${st.class_name || 'Lớp'})`;
          return o;
        })
      );
    }
  } catch (e) {
    console.error('Lỗi tải danh sách sinh viên:', e);
  }
}

// CẤP TÀI KHOẢN SINH VIÊN MỚI THEO MSV
async function createNewStudent() {
  try {
    const sId = document.querySelector('#newStudentId').value.trim();
    const name = document.querySelector('#newStudentName').value.trim();
    const cls = document.querySelector('#newStudentClass').value.trim();
    const pass = document.querySelector('#newStudentPass').value.trim() || 'SVdemo123!';

    if (!sId || !name) {
      throw Error('Vui lòng nhập Mã Sinh Viên (MSV) và Họ Tên sinh viên.');
    }

    let res = await api('/api/students', 'POST', {
      student_id: sId,
      name: name,
      class_name: cls,
      password: pass
    });

    show(`✓ Đã lưu sinh viên vào Database & cấp tài khoản thành công!\nTài khoản / MSV: ${sId} | Mật khẩu: ${pass}\nĐịa chỉ ví Blockchain: ${res.student.wallet}`);
    document.querySelector('#newStudentId').value = '';
    document.querySelector('#newStudentName').value = '';
    await loadRegisteredStudents();
  } catch (e) {
    show('Lỗi cấp tài khoản: ' + e.message);
  }
}

// XEM DANH SÁCH CHI TIẾT SINH VIÊN TRONG BUỔI ĐIỂM DANH (CHO GIẢNG VIÊN)
async function viewSessionAttendees(sessionId, sessionTitle) {
  currentViewingSessionId = sessionId;
  document.querySelector('#modalSessionTitle').textContent = 'Danh Sách Điểm Danh: ' + sessionTitle;
  document.querySelector('#modalSessionSub').textContent = 'Buổi học ID #' + sessionId + ' · Dữ liệu được liên kết mật mã trên Blockchain';
  document.querySelector('#attendeesModal').classList.remove('hidden');
  await refreshAttendeesList();
}

async function refreshAttendeesList() {
  if (!currentViewingSessionId) return;
  try {
    let d = await api('/api/sessions/' + currentViewingSessionId + '/attendees');
    const total = d.attendees.length;
    const present = d.attendees.filter(a => a.is_present).length;
    const absent = total - present;

    document.querySelector('#statRosterTotal').textContent = total;
    document.querySelector('#statRosterPresent').textContent = present;
    document.querySelector('#statRosterAbsent').textContent = absent;

    const rows = d.attendees.map((st, idx) => {
      const statusBadge = st.is_present
        ? `<span class="badge badge-valid">✓ Có mặt</span>`
        : `<span class="badge badge-tamper">✗ Chưa có mặt</span>`;

      const timeText = st.occurred_at ? new Date(st.occurred_at).toLocaleTimeString('vi-VN') : '—';
      const hashText = st.event_hash ? `<span class="wallet-pill" title="${st.event_hash}">${st.event_hash.substring(0, 16)}...</span>` : '—';

      const actionHtml = st.is_present
        ? `<span style="color:var(--success); font-size:12px; font-weight:600;">Đã ghi nhận On-Chain</span>`
        : `<button class="btn-sm btn-accent" onclick="manualCheckIn('${st.student_username}')">✍️ Điểm danh bù</button>`;

      return [
        idx + 1,
        `<b>${st.student_id || st.student_username}</b>`,
        st.name,
        st.class_name || 'CNTT2.K25',
        { html: statusBadge },
        timeText,
        { html: hashText },
        { html: actionHtml }
      ];
    });

    renderTable('#attendeesTableBox', ['STT', 'Mã SV (Tài Khoản)', 'Họ và Tên', 'Lớp', 'Trạng thái', 'Thời gian', 'Mã Hash Blockchain', 'Thao tác'], rows, 'Chưa có sinh viên nào ghi danh trong lớp.');
  } catch (e) {
    show('Lỗi tải danh sách: ' + e.message);
  }
}

async function manualCheckIn(studentUsername) {
  if (!currentViewingSessionId) return;
  try {
    let d = await api('/api/sessions/' + currentViewingSessionId + '/manual-checkin', 'POST', {
      student_username: studentUsername
    });
    show('✓ ' + d.message);
    await refreshAttendeesList();
    await loadSessions();
  } catch (e) {
    show('Lỗi điểm danh bù: ' + e.message);
  }
}

function closeAttendeesModal() {
  document.querySelector('#attendeesModal').classList.add('hidden');
  currentViewingSessionId = null;
}

async function login() {
  try {
    let d = await api('/api/login', 'POST', {
      username: username.value.trim(),
      password: password.value
    });
    auth = d.token;
    localStorage.setItem('attendance-token', auth);
    document.querySelector('#authMessage').textContent = '';
    setRole(d);
    await loadClasses();
    await loadSessions();
    show('Đăng nhập thành công với vai trò: ' + (d.role === 'lecturer' ? 'Giảng viên' : 'Sinh viên (MSV: ' + (d.student_id || d.username) + ')'));
  } catch (e) {
    document.querySelector('#authMessage').textContent = e.message;
  }
}

function logout() {
  stopQrScanner();
  if (sessionsRefreshTimer) {
    clearInterval(sessionsRefreshTimer);
    sessionsRefreshTimer = null;
  }
  auth = '';
  currentUser = null;
  currentQrData = null;
  currentViewingSessionId = null;
  localStorage.removeItem('attendance-token');
  document.querySelector('#userHeader').classList.add('hidden');
  document.querySelector('#dashboard').classList.add('hidden');
  document.querySelector('#authSection').classList.remove('hidden');
  document.querySelector('#lecturerPanel').hidden = true;
  document.querySelector('#studentPanel').hidden = true;
  document.querySelector('#classPanel').hidden = true;
  document.querySelector('#mainTabs').style.display = 'none';
  document.querySelector('#attendeesModal').classList.add('hidden');
  document.querySelector('#projectorModal').classList.add('hidden');
  document.querySelector('#username').value = '';
  document.querySelector('#password').value = '';
  document.querySelector('#authMessage').textContent = 'Đã đăng xuất.';
}

async function showSelectedClass() {
  const selected = document.querySelector('#classes').selectedOptions[0];
  document.querySelector('#selectedClass').textContent = selected
    ? `Môn: ${selected.dataset.className} (${selected.dataset.courseCode || ''}) | Mã lớp: ${selected.dataset.classCode || ''}`
    : '';
}

async function loadClasses() {
  try {
    let d = await api('/api/classes');
    const select = document.querySelector('#classes');
    select.replaceChildren(...d.classes.map(c => {
      let o = document.createElement('option');
      o.value = c.id;
      o.dataset.courseCode = c.course_code;
      o.dataset.classCode = c.code;
      o.dataset.className = c.name;
      o.textContent = `${c.course_code || 'Môn'} — ${c.code} — ${c.name}`;
      return o;
    }));
    showSelectedClass();
  } catch (e) {
    show(e.message);
  }
}

async function createClass() {
  try {
    await api('/api/classes', 'POST', {
      course_code: document.querySelector('#courseCode').value,
      code: document.querySelector('#classCode').value,
      name: document.querySelector('#className').value
    });
    await loadClasses();
    await loadSessions();
    show('Đã khởi tạo môn học và lớp học thành công.');
  } catch (e) {
    show(e.message);
  }
}

async function enrollStudent() {
  try {
    let studentInput = document.querySelector('#studentUsername').value.trim();
    if (!studentInput) {
      const select = document.querySelector('#availableStudentsSelect');
      if (select && select.value) studentInput = select.value;
    }
    if (!studentInput) throw Error('Vui lòng chọn hoặc nhập Mã Sinh Viên cần ghi danh.');

    await api('/api/classes/' + document.querySelector('#classes').value + '/enrollments', 'POST', {
      student_username: studentInput
    });
    document.querySelector('#studentUsername').value = '';
    show(`Đã ghi danh sinh viên ${studentInput} vào lớp học thành công.`);
  } catch (e) {
    show(e.message);
  }
}

let currentQrData = null;

async function createSession() {
  try {
    let d = await api('/api/sessions', 'POST', {
      class_id: +document.querySelector('#classes').value,
      title: document.querySelector('#sessionTitle').value
    });
    currentQrData = d;
    const qrImg = document.querySelector('#qr');
    qrImg.src = '/api/sessions/' + d.id + '/qr.svg?token=' + encodeURIComponent(d.qr_token);
    document.querySelector('#qrLink').textContent = d.qr_url;
    document.querySelector('#attendanceCodeDisplay').textContent = d.attendance_code;
    document.querySelector('#liveSessionTitle').textContent = 'Buổi học: ' + d.title;
    document.querySelector('#liveSessionCard').style.display = 'block';

    await loadSessions();
    show('Đã mở buổi học. Mã điểm danh PIN: ' + d.attendance_code);
  } catch (e) {
    show(e.message);
  }
}

function openProjectorModal() {
  if (!currentQrData) return;
  document.querySelector('#modalTitle').textContent = currentQrData.title;
  document.querySelector('#modalPin').textContent = currentQrData.attendance_code;
  document.querySelector('#modalQrImg').src = document.querySelector('#qr').src;
  document.querySelector('#projectorModal').classList.remove('hidden');
}

function closeProjectorModal() {
  document.querySelector('#projectorModal').classList.add('hidden');
}

function stopQrScanner() {
  if (qrScanFrame) cancelAnimationFrame(qrScanFrame);
  qrScanFrame = 0;
  if (qrCameraStream) {
    qrCameraStream.getTracks().forEach(track => track.stop());
    qrCameraStream = null;
  }
  const video = document.querySelector('#qrVideo');
  video.pause();
  video.srcObject = null;
  document.querySelector('#qrScanner').classList.add('hidden');
  document.querySelector('#qrScanButton').disabled = false;
}

function getAttendanceCode(rawValue) {
  let scannedValue = rawValue.trim();
  if (!scannedValue) throw Error('Ảnh không chứa mã QR.');
  try {
    const scannedUrl = new URL(scannedValue, location.href);
    if (scannedUrl.searchParams.has('code')) {
      scannedValue = scannedUrl.searchParams.get('code') || '';
    }
  } catch {}
  if (!scannedValue) throw Error('QR không chứa mã điểm danh.');
  if (/^https?:\/\//i.test(scannedValue)) {
    throw Error('QR này không chứa đường dẫn điểm danh.');
  }
  return scannedValue;
}

function useScannedQr(rawValue) {
  document.querySelector('#qrToken').value = getAttendanceCode(rawValue);
  stopQrScanner();
  show('Đã nhận mã QR. Nhấn “Xác nhận Điểm danh” để hoàn tất.');
}

async function scanQrImage(event) {
  const input = event.target;
  const image = input.files && input.files[0];
  input.value = '';
  if (!image) return;
  if (image.size > 4 * 1024 * 1024) {
    show('Ảnh QR phải nhỏ hơn 4 MB.');
    return;
  }
  try {
    const imageData = await new Promise((resolve, reject) => {
      const reader = new FileReader();
      reader.onload = () => resolve(reader.result);
      reader.onerror = () => reject(Error('Không thể đọc tệp ảnh.'));
      reader.readAsDataURL(image);
    });
    const result = await api('/api/attendance/scan-image', 'POST', { image: imageData });
    useScannedQr(result.qr_token);
  } catch (e) {
    show('Không đọc được ảnh QR: ' + e.message);
  }
}

async function startQrScanner() {
  if (!currentUser || currentUser.role !== 'student') return;
  if (!window.isSecureContext || !navigator.mediaDevices || !navigator.mediaDevices.getUserMedia) {
    show('Camera cần chạy trên HTTPS hoặc localhost. Điện thoại có thể dùng ứng dụng Camera để quét mã QR trên màn hình giảng viên.');
    return;
  }
  if (typeof window.BarcodeDetector !== 'function') {
    show('Trình duyệt chưa hỗ trợ quét QR trực tiếp. Hãy dùng ứng dụng Camera của điện thoại để quét mã QR.');
    return;
  }

  stopQrScanner();
  const scanner = document.querySelector('#qrScanner');
  const message = document.querySelector('#qrScannerMessage');
  const video = document.querySelector('#qrVideo');
  const button = document.querySelector('#qrScanButton');
  scanner.classList.remove('hidden');
  message.textContent = 'Đang mở camera. Hãy hướng camera vào mã QR của buổi học.';
  button.disabled = true;

  try {
    const detector = new window.BarcodeDetector({ formats: ['qr_code'] });
    const stream = await navigator.mediaDevices.getUserMedia({
      video: { facingMode: { ideal: 'environment' } },
      audio: false
    });
    if (!currentUser || currentUser.role !== 'student') {
      stream.getTracks().forEach(track => track.stop());
      stopQrScanner();
      return;
    }
    qrCameraStream = stream;
    video.srcObject = qrCameraStream;
    await video.play();
    if (video.readyState < 2) {
      await new Promise(resolve => video.addEventListener('loadeddata', resolve, { once: true }));
    }

    const scanFrame = async () => {
      if (!qrCameraStream) return;
      try {
        const codes = await detector.detect(video);
        if (codes.length) {
          useScannedQr(codes[0].rawValue);
          return;
        }
      } catch (e) {
        stopQrScanner();
        show('Không đọc được mã QR: ' + e.message);
        return;
      }
      qrScanFrame = requestAnimationFrame(scanFrame);
    };
    qrScanFrame = requestAnimationFrame(scanFrame);
  } catch (e) {
    stopQrScanner();
    show('Không mở được camera: ' + e.message);
  }
}

async function closeSession() {
  try {
    const sId = document.querySelector('#sessionId').value.trim();
    if (!sId) throw Error('Vui lòng nhập ID buổi học cần đóng.');
    await api('/api/sessions/' + sId + '/close', 'POST', {});
    await loadSessions();
    document.querySelector('#liveSessionCard').style.display = 'none';
    show('Đã đóng buổi học số #' + sId + '. Sổ điểm danh đã được khóa On-Chain.');
  } catch (e) {
    show(e.message);
  }
}

async function loadSessions() {
  try {
    let [classesData, sessionsData] = await Promise.all([
      api('/api/classes'),
      api('/api/sessions')
    ]);
    renderSessions(classesData.classes, sessionsData.sessions);
  } catch (e) {
    show(e.message);
  }
}

async function checkIn() {
  try {
    const input = document.querySelector('#qrToken').value.trim();
    if (!input) throw Error('Vui lòng nhập mã PIN hoặc dán mã QR Token.');
    const payload = /^\d{6}$/.test(input) ? { attendance_code: input } : { qr_token: input };
    let d = await api('/api/attendance', 'POST', payload);
    const stName = currentUser ? currentUser.name : 'Sinh viên';
    const stId = currentUser ? (currentUser.student_id || currentUser.username) : '';
    show(`✓ Điểm danh thành công cho sinh viên: ${stName} (MSV: ${stId})!\nGiao dịch đã được ký và đóng gói vào Blockchain.`);
    document.querySelector('#qrToken').value = '';
    await loadSessions();
  } catch (e) {
    show('Lỗi điểm danh: ' + e.message);
  }
}

// BLOCKCHAIN EXPLORER FUNCTIONS
async function loadBlockchainExplorer() {
  try {
    let data = await api('/api/blockchain/explorer');
    document.querySelector('#statTotalBlocks').textContent = data.total_blocks;
    document.querySelector('#statTotalTxs').textContent = data.total_attendance_events;

    const statusEl = document.querySelector('#statChainStatus');
    const alertBox = document.querySelector('#tamperAlertBox');

    if (data.valid) {
      statusEl.textContent = 'HỢP LỆ (100%)';
      statusEl.style.color = 'var(--success)';
      alertBox.innerHTML = `
        <div style="background:#ecfdf5; border-left:4px solid var(--success); padding:12px; border-radius:6px; margin-bottom:16px;">
          <b style="color:var(--success);">✅ XÁC THỰC THÀNH CÔNG:</b> Toàn bộ các khối liên kết mật mã hoàn hảo. Không có dấu hiệu can thiệp hay sửa đổi trái phép!
        </div>`;
    } else {
      statusEl.textContent = 'BỊ LỖI / CAN THIỆP';
      statusEl.style.color = 'var(--danger)';
      alertBox.innerHTML = `
        <div style="background:#fff1f2; border-left:4px solid var(--danger); padding:12px; border-radius:6px; margin-bottom:16px;">
          <b style="color:var(--danger);">⚠️ PHÁT HIỆN SAI LỆCH MẬT MÃ:</b> Chuỗi khối bị đứt gãy tại Khối ID #${data.broken_at}. Dữ liệu tại khối này đã bị chỉnh sửa bất hợp pháp!
        </div>`;
    }

    const container = document.querySelector('#blockchainBlocks');
    container.replaceChildren();

    data.blocks.forEach((block, idx) => {
      const card = document.createElement('div');
      card.className = 'block-card ' + (block.status === 'VALID' ? 'valid' : 'tampered');

      let txsHtml = block.transactions.map(tx => {
        if (tx.type === 'CONTRACT_INITIALIZATION') {
          return `<div class="tx-item">
            <b>📜 Khởi tạo Smart Contract:</b> ${tx.contract}<br>
            <span style="font-size:12px; color:var(--muted);">Deployer: ${tx.sender}</span>
          </div>`;
        }
        return `<div class="tx-item">
          <b>👤 Sinh viên:</b> ${tx.student_name} (MSV: <b>${tx.student_id}</b> · Lớp: ${tx.class_name})<br>
          <b>Môn học:</b> ${tx.course_name} (${tx.session_title})<br>
          <span style="font-size:11px; font-family:monospace; color:var(--muted);">Ví: ${tx.student_wallet} | Chữ ký: ${tx.signature}</span>
        </div>`;
      }).join('');

      card.innerHTML = `
        <div style="display:flex; justify-content:space-between; align-items:center; margin-bottom:10px; border-bottom:1px dashed #e2e8f0; padding-bottom:6px;">
          <div>
            <b style="color:var(--primary); font-size:15px;">${block.title}</b>
            <span style="font-size:12px; color:var(--muted); margin-left:8px;">${new Date(block.timestamp).toLocaleString('vi-VN')}</span>
          </div>
          <div>
            <span class="badge ${block.status === 'VALID' ? 'badge-valid' : 'badge-tamper'}">
              ${block.status === 'VALID' ? 'Chữ ký hợp lệ ✓' : 'LỖI MÃ BĂM ✗'}
            </span>
            <span class="badge" style="background:#e2e8f0;">Nonce: ${block.nonce}</span>
          </div>
        </div>
        <div style="font-size:13px;">
          <div><b>Previous Hash:</b> <div class="block-hash-box">${block.previous_hash}</div></div>
          <div><b>Block Hash:</b> <div class="block-hash-box" style="font-weight:bold; color:var(--primary);">${block.block_hash}</div></div>
        </div>
        <div style="margin-top:10px;">
          <b style="font-size:13px;">Giao dịch điểm danh trong khối (${block.transactions.length}):</b>
          ${txsHtml}
        </div>
      `;

      container.append(card);
    });

  } catch (e) {
    show(e.message);
  }
}

async function simulateTamper() {
  try {
    const note = 'Lưu ý: đây là thao tác CỐ Ý sửa dữ liệu để trình diễn cơ chế phát hiện. ' +
      'Trong triển khai thật endpoint này bị tắt (chỉ bật khi ATTENDANCE_DEMO=1 và đăng nhập giảng viên).';
    if (!window.confirm(note + '\n\nTiếp tục giả lập sửa dữ liệu?')) return;
    let d = await api('/api/blockchain/tamper', 'POST', {});
    show('⚡ ' + d.message);
    await loadBlockchainExplorer();
  } catch (e) {
    show(e.message);
  }
}

async function repairBlockchain() {
  try {
    // Bước 1: yêu cầu API báo cáo điểm gãy (chưa ghi gì)
    let report = await api('/api/blockchain/repair', 'POST', {});
    if (!report.requires_confirmation || report.applied) {
      show(report.message);
      await loadBlockchainExplorer();
      return;
    }
    const detail = (report.details && report.details[0]) || {};
    const where = detail.session_title
      ? ` (buổi "${detail.session_title}", SV ${detail.student_username})`
      : '';
    const ok = window.confirm(
      `${report.message}\n\nKhối sai lệch: #${report.broken_block}${where}\n\n` +
      'Ghi chú: thao tác này chỉ tái tạo lại chữ ký số/liên kết chuỗi cho dữ liệu đã có, ' +
      'không lấy lại được bản ghi đã bị xoá.'
    );
    if (!ok) {
      show('Đã hủy khôi phục. Chuỗi vẫn đang ở trạng thái sai lệch.');
      return;
    }
    // Bước 2: xác nhận ghi lại chuỗi
    let d = await api('/api/blockchain/repair', 'POST', { confirm: true });
    show(d.message);
    await loadBlockchainExplorer();
  } catch (e) {
    show(e.message);
  }
}

if (auth) {
  api('/api/me').then(d => {
    setRole(d);
    loadClasses();
    loadSessions();
  }).catch(logout);
}
if (urlCode) show('Đã nhận mã QR. Đăng nhập tài khoản sinh viên (MSV) để xác nhận điểm danh.');
</script>
</body>
</html>
"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AttendanceDemo/2.2"

    def log_message(self, fmt: str, *args: object) -> None:
        target = self.requestline.split(" ")[1]
        message = (fmt % args).replace(target, urlparse(target).path)
        print(f"{self.address_string()} - {message}")

    def respond(self, status: int, data: object, content_type: str = "application/json") -> None:
        body = data if isinstance(data, bytes) else json.dumps(
            data, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header(
            "Content-Type",
            content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/json" else ""),
        )
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self, max_bytes: int = 32_768) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > max_bytes:
            raise ValueError("Nội dung gửi lên quá lớn.")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("Dữ liệu gửi lên phải là JSON object.")
        return payload

    def current_user(self) -> UserRecord | None:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        if not token or token not in SESSIONS:
            return None
        sess = SESSIONS[token]
        username = sess["username"]
        with connect() as db:
            user = find_user_in_db(db, username)
        if not user:
            return None
        # Return effective username that was logged in with
        eff_username = sess.get("effective_username") or user["username"]
        return UserRecord.from_row(str(eff_username), user)

    def require_role(self, role: str) -> UserRecord | None:
        user = self.current_user()
        if user is None:
            self.respond(401, {"error": "Vui lòng đăng nhập."})
            return None
        if user.role != role:
            self.respond(403, {"error": "Bạn không có quyền thực hiện thao tác này."})
            return None
        return user

    def public_base_url(self) -> str:
        """URL công khai để nhúng vào QR.

        - Có ATTENDANCE_BASE_URL (và không phải localhost) thì dùng luôn.
        - Nếu server đang phục vụ trên địa chỉ thật (LAN hoặc tên miền) thì suy ra từ Host
          để link/QR gửi cho người khác vẫn mở được.
        - Ngược lại giữ BASE_URL (localhost) cho máy đang chạy.
        """
        host = (self.headers.get("Host") or "").strip()
        # Port tường minh: "host:8000" (không tính IPv6 dạng [::1]:8000)
        port_part = host.rsplit(":", 1)[-1] if host.count(":") == 1 else ""
        bare_host = host.split(":")[0].strip("[]").lower()
        is_local = bare_host in ("localhost", "127.0.0.1", "::1", "") or bare_host.startswith("127.")
        if host and not is_local and bare_host not in ("0.0.0.0", "::"):
            forwarded = self.headers.get("X-Forwarded-Proto", "").split(",")[0].strip().lower()
            if forwarded in ("http", "https"):
                scheme = forwarded
            else:
                # Không có port riêng => thường là sau proxy HTTPS (80/443)
                scheme = "http" if port_part not in ("", "80", "443") else "https"
            return f"{scheme}://{host}".rstrip("/")
        return BASE_URL

    def require_demo_mode(self) -> bool:
        """Chỉ cho phép các thao tác phá hoại khi bật chế độ demo và là giảng viên."""
        if not DEMO_MODE:
            self.respond(404, {"error": "Không tìm thấy tài nguyên."})
            return False
        if self.require_role("lecturer") is None:
            return False
        return True

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/":
            self.respond(200, HTML.encode("utf-8"), "text/html")
            return

        if path == "/api/me":
            user = self.current_user()
            if not user:
                self.respond(401, {"error": "Vui lòng đăng nhập."})
            else:
                self.respond(200, {
                    "username": user.effective_username,
                    "canonical_username": user.username,
                    "role": user.role,
                    "name": user.name,
                    "student_id": user.student_id,
                    "class_name": user.class_name,
                    "wallet": user.wallet_address,
                })
            return

        # DANH SÁCH TẤT CẢ SINH VIÊN TRONG HỆ THỐNG
        if path == "/api/students":
            user = self.require_role("lecturer")
            if not user:
                return
            with connect() as db:
                rows = db.execute(
                    """SELECT username, student_id, name, class_name, wallet_address, created_at
                    FROM users WHERE role = 'student' ORDER BY student_id ASC"""
                ).fetchall()
            self.respond(200, {"students": [dict(r) for r in rows]})
            return

        if path == "/api/classes":
            user = self.current_user()
            if not user:
                self.respond(401, {"error": "Vui lòng đăng nhập."})
                return
            if user.role not in ("lecturer", "student"):
                self.respond(403, {"error": "Bạn không có quyền thực hiện thao tác này."})
                return
            with connect() as db:
                if user.role == "lecturer":
                    rows = db.execute(
                        """SELECT id, code, course_code, name FROM classes
                        WHERE lecturer_username = ? ORDER BY id""",
                        (user.username,),
                    ).fetchall()
                else:
                    st_keys = (user.effective_username, user.username, user.alias or user.effective_username)
                    rows = db.execute(
                        """SELECT DISTINCT c.id, c.code, c.course_code, c.name FROM classes c
                        JOIN enrollments e ON e.class_id = c.id
                        WHERE e.student_username IN (?, ?, ?) ORDER BY c.id""",
                        st_keys,
                    ).fetchall()
            self.respond(200, {"classes": [dict(row) for row in rows]})
            return

        if path == "/api/sessions":
            user = self.current_user()
            if not user:
                self.respond(401, {"error": "Vui lòng đăng nhập."})
                return
            with connect() as db:
                if user.role == "lecturer":
                    query = """SELECT s.id, s.class_id, c.code AS class_code,
                        c.course_code, c.name AS class_name, s.title, s.attendance_code,
                        s.starts_at, s.ends_at, s.status,
                        (SELECT COUNT(*) FROM attendance_events a WHERE a.session_id=s.id)
                            AS attended_count,
                        (SELECT COUNT(DISTINCT e.student_username) FROM enrollments e WHERE e.class_id=c.id)
                            AS enrolled_count
                        FROM sessions s JOIN classes c ON c.id=s.class_id
                        WHERE c.lecturer_username = ?"""
                    params = (user.username,)
                else:
                    st_keys = (user.effective_username, user.username, user.alias or user.effective_username)
                    query = """SELECT s.id, s.class_id, c.code AS class_code,
                        c.course_code, c.name AS class_name, s.title,
                        s.starts_at, s.ends_at, s.status,
                        CASE WHEN EXISTS (
                            SELECT 1 FROM attendance_events a
                            WHERE a.session_id=s.id AND a.student_username IN (?, ?, ?)
                        ) THEN 'present'
                        WHEN s.status='closed' THEN 'absent'
                        ELSE 'pending' END AS attendance_status
                        FROM sessions s JOIN classes c ON c.id=s.class_id
                        WHERE EXISTS (
                            SELECT 1 FROM enrollments e
                            WHERE e.class_id=c.id AND e.student_username IN (?, ?, ?)
                        )"""
                    params = st_keys + st_keys
                rows = db.execute(query + " ORDER BY s.id DESC", params).fetchall()
            self.respond(200, {"sessions": [dict(row) for row in rows]})
            return

        # DANH SÁCH SINH VIÊN TRONG BUỔI ĐIỂM DANH (ATTENDEES ROSTER)
        if path.startswith("/api/sessions/") and path.endswith("/attendees"):
            user = self.require_role("lecturer")
            if not user:
                return
            parts = path.split("/")
            try:
                session_id = int(parts[3])
                with connect() as db:
                    session = db.execute(
                        """SELECT s.id, s.title, s.status, c.id AS class_id, c.code AS class_code, c.name AS class_name
                        FROM sessions s JOIN classes c ON c.id = s.class_id
                        WHERE s.id = ? AND c.lecturer_username = ?""",
                        (session_id, user.username),
                    ).fetchone()
                    if not session:
                        self.respond(404, {"error": "Không tìm thấy buổi học thuộc quyền giảng viên."})
                        return

                    enrolled_students = db.execute(
                        """SELECT DISTINCT e.student_username FROM enrollments e
                        WHERE e.class_id = ?""",
                        (session["class_id"],),
                    ).fetchall()

                    seen_users = set()
                    attendees = []

                    for er in enrolled_students:
                        u_obj = find_user_in_db(db, er["student_username"])
                        if not u_obj or u_obj["role"] != "student":
                            continue
                        canon_name = u_obj["username"]
                        if canon_name in seen_users:
                            continue
                        seen_users.add(canon_name)

                        # Check attendance
                        att = db.execute(
                            """SELECT occurred_at, event_hash, actor_username FROM attendance_events
                            WHERE session_id = ? AND student_username IN (?, ?)""",
                            (session_id, canon_name, u_obj.get("alias") or canon_name),
                        ).fetchone()

                        attendees.append({
                            "student_username": er["student_username"],
                            "canonical_username": canon_name,
                            "alias": u_obj.get("alias") or "",
                            "name": u_obj["name"],
                            "student_id": u_obj.get("student_id") or canon_name,
                            "class_name": u_obj.get("class_name") or "CNTT2.K25",
                            "wallet": u_obj.get("wallet_address") or "",
                            "is_present": bool(att),
                            "occurred_at": att["occurred_at"] if att else None,
                            "event_hash": att["event_hash"] if att else None,
                            "actor_username": att["actor_username"] if att else None,
                        })

                self.respond(200, {
                    "session_id": session["id"],
                    "session_title": session["title"],
                    "class_code": session["class_code"],
                    "class_name": session["class_name"],
                    "attendees": attendees,
                })
            except (ValueError, IndexError):
                self.respond(400, {"error": "Mã buổi học không hợp lệ."})
            return

        if path.startswith("/api/sessions/") and path.endswith("/qr.svg"):
            user = self.require_role("lecturer")
            if not user:
                return
            parts = path.split("/")
            try:
                session_id = int(parts[3])
                token = parse_qs(parsed.query)["token"][0]
                with connect() as db:
                    owned = db.execute(
                        """SELECT 1 FROM sessions s JOIN classes c ON c.id=s.class_id
                        WHERE s.id=? AND c.lecturer_username=?""",
                        (session_id, user.username),
                    ).fetchone()
                if not owned or parse_qr_token(token) != session_id:
                    raise ValueError
                self.respond(200, qr_svg(f"{self.public_base_url()}/?code={token}"), "image/svg+xml")
            except (ValueError, KeyError, IndexError):
                self.respond(400, {"error": "Không thể tạo mã QR cho buổi học này."})
            return

        if path == "/api/attendance/mine":
            user = self.require_role("student")
            if not user:
                return
            st_keys = (user.effective_username, user.username, user.alias or user.effective_username)
            with connect() as db:
                rows = db.execute(
                    """SELECT a.session_id, s.title, c.code AS class_code,
                    a.occurred_at, a.event_hash FROM attendance_events a
                    JOIN sessions s ON s.id=a.session_id JOIN classes c ON c.id=s.class_id
                    WHERE a.student_username IN (?, ?, ?) ORDER BY a.id DESC""",
                    st_keys,
                ).fetchall()
            self.respond(200, {"records": [dict(row) for row in rows]})
            return

        if path == "/api/audit/verify":
            user = self.require_role("lecturer")
            if not user:
                return
            with connect() as db:
                valid, broken_at = verify_chain(db)
                count = db.execute("SELECT COUNT(*) AS n FROM attendance_events").fetchone()["n"]
            self.respond(200, {"valid": valid, "events": count, "broken_at": broken_at})
            return

        if path == "/api/blockchain/explorer":
            user = self.require_role("lecturer")
            if not user:
                return
            with connect() as db:
                data = get_blockchain_ledger(db)
            self.respond(200, data)
            return

        self.respond(404, {"error": "Không tìm thấy tài nguyên."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/attendance/scan-image":
            user = self.require_role("student")
            if not user:
                return
            try:
                data = self.read_json(QR_IMAGE_MAX_JSON_BYTES)
                qr_token = decode_attendance_qr_image(str(data.get("image", "")))
            except (ValueError, json.JSONDecodeError) as error:
                self.respond(400, {"error": str(error)})
                return
            self.respond(200, {"qr_token": qr_token})
            return

        try:
            data = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.respond(400, {"error": str(error)})
            return

        if path == "/api/login":
            username_in = str(data.get("username", "")).strip()
            password_in = str(data.get("password", ""))
            with connect() as db:
                user, ambiguous_name = find_login_user_in_db(db, username_in)
            if ambiguous_name:
                self.respond(409, {"error": "Có nhiều tài khoản trùng họ tên. Vui lòng liên hệ giảng viên."})
                return
            if not user or not hmac.compare_digest(password_in, user["password"]):
                self.respond(401, {"error": "Họ tên/tài khoản hoặc mật khẩu không chính xác."})
                return

            token = secrets.token_urlsafe(32)
            canonical = user["username"]
            eff = username_in if username_in in (user.get("alias"), canonical) else canonical
            SESSIONS[token] = {
                "username": canonical,
                "effective_username": eff,
            }

            self.respond(200, {
                "token": token,
                "username": eff,
                "canonical_username": canonical,
                "role": user["role"],
                "name": user["name"],
                "student_id": user.get("student_id") or canonical,
                "class_name": user.get("class_name") or "",
                "wallet": user.get("wallet_address") or "",
            })
            return

        # CẤP TÀI KHOẢN SINH VIÊN MỚI THEO MSV (LƯU DATABASE)
        if path == "/api/students":
            user = self.require_role("lecturer")
            if not user:
                return
            student_id = str(data.get("student_id", "")).strip()
            name = str(data.get("name", "")).strip()
            class_name = str(data.get("class_name", "")).strip() or "CNTT2.K25"
            password = str(data.get("password", "")).strip() or "SVdemo123!"

            if not student_id or not name:
                self.respond(400, {"error": "Mã sinh viên (MSV) và Họ tên không được để trống."})
                return

            with connect() as db:
                exists = find_user_in_db(db, student_id)
                if exists:
                    self.respond(409, {"error": f"Sinh viên với MSV {student_id} đã tồn tại trong hệ thống."})
                    return

                wallet = generate_wallet(student_id)
                db.execute(
                    """
                    INSERT INTO users (
                        username, alias, password, role, name, student_id, class_name, wallet_address, created_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (student_id, student_id, password, "student", name, student_id, class_name, wallet, now_iso()),
                )

            self.respond(201, {
                "message": f"Đã cấp tài khoản thành công cho sinh viên {name} theo MSV {student_id}.",
                "student": {
                    "username": student_id,
                    "student_id": student_id,
                    "name": name,
                    "class_name": class_name,
                    "wallet": wallet,
                },
            })
            return

        if path == "/api/classes":
            user = self.require_role("lecturer")
            if not user:
                return
            code = str(data.get("code", "")).strip()
            course_code = str(data.get("course_code", "")).strip()
            name = str(data.get("name", "")).strip()
            if not code or not course_code or not name:
                self.respond(
                    400,
                    {"error": "Mã môn học, mã lớp và tên môn học không được để trống."},
                )
                return
            try:
                with connect() as db:
                    cursor = db.execute(
                        """INSERT INTO classes(code, course_code, name, lecturer_username)
                        VALUES(?, ?, ?, ?)""",
                        (code, course_code, name, user.username),
                    )
                self.respond(
                    201,
                    {
                        "id": cursor.lastrowid,
                        "code": code,
                        "course_code": course_code,
                        "name": name,
                    },
                )
            except sqlite3.IntegrityError:
                self.respond(409, {"error": "Mã lớp đã tồn tại."})
            return

        if path.startswith("/api/classes/") and path.endswith("/enrollments"):
            user = self.require_role("lecturer")
            if not user:
                return
            try:
                class_id = int(path.split("/")[3])
            except (ValueError, IndexError):
                self.respond(400, {"error": "Mã lớp không hợp lệ."})
                return
            student_raw = str(data.get("student_username", "")).strip()
            with connect() as db:
                st_user = find_user_in_db(db, student_raw)
                if not st_user or st_user["role"] != "student":
                    self.respond(400, {"error": "Mã sinh viên hoặc tài khoản không tồn tại."})
                    return

                owned = db.execute(
                    "SELECT 1 FROM classes WHERE id=? AND lecturer_username=?",
                    (class_id, user.username),
                ).fetchone()
                if not owned:
                    self.respond(404, {"error": "Không tìm thấy lớp thuộc quyền giảng viên."})
                    return

                canonical_st = st_user["username"]
                alias_st = st_user.get("alias")

                try:
                    db.execute(
                        "INSERT INTO enrollments(class_id, student_username) VALUES(?, ?)",
                        (class_id, student_raw),
                    )
                    if student_raw != canonical_st:
                        db.execute(
                            "INSERT OR IGNORE INTO enrollments(class_id, student_username) VALUES(?, ?)",
                            (class_id, canonical_st),
                        )
                    if alias_st and alias_st != student_raw:
                        db.execute(
                            "INSERT OR IGNORE INTO enrollments(class_id, student_username) VALUES(?, ?)",
                            (class_id, alias_st),
                        )
                except sqlite3.IntegrityError:
                    self.respond(409, {"error": "Sinh viên đã được ghi danh vào lớp."})
                    return
            self.respond(201, {"class_id": class_id, "student_username": student_raw})
            return

        if path == "/api/sessions":
            user = self.require_role("lecturer")
            if not user:
                return
            title = str(data.get("title", "")).strip()
            try:
                class_id = int(data.get("class_id", 0))
            except (TypeError, ValueError):
                class_id = 0
            if not title or not class_id:
                self.respond(400, {"error": "Cần chọn lớp và nhập tên buổi học."})
                return
            with connect() as db:
                owned = db.execute(
                    "SELECT 1 FROM classes WHERE id=? AND lecturer_username=?",
                    (class_id, user.username),
                ).fetchone()
                if not owned:
                    self.respond(404, {"error": "Không tìm thấy lớp thuộc quyền giảng viên."})
                    return
                cursor = db.execute(
                    """INSERT INTO sessions(
                        class_id, title, starts_at, ends_at, attendance_code
                    ) VALUES(?, ?, ?, ?, ?)""",
                    (
                        class_id,
                        title,
                        now_iso(),
                        "2099-12-31T23:59:59+00:00",
                        new_attendance_code(db),
                    ),
                )
                session_id = cursor.lastrowid
                attendance_code = db.execute(
                    "SELECT attendance_code FROM sessions WHERE id=?",
                    (session_id,),
                ).fetchone()["attendance_code"]
            token = make_qr_token(session_id)
            self.respond(
                201,
                {
                    "id": session_id,
                    "title": title,
                    "qr_token": token,
                    "attendance_code": attendance_code,
                    "qr_url": f"{self.public_base_url()}/?code={token}",
                    "expires_in_seconds": TOKEN_LIFETIME,
                },
            )
            return

        if path.startswith("/api/sessions/") and path.endswith("/close"):
            user = self.require_role("lecturer")
            if not user:
                return
            try:
                session_id = int(path.split("/")[3])
            except (ValueError, IndexError):
                self.respond(400, {"error": "Mã buổi học không hợp lệ."})
                return
            with connect() as db:
                cursor = db.execute(
                    """UPDATE sessions SET status='closed' WHERE id=? AND class_id IN
                    (SELECT id FROM classes WHERE lecturer_username=?) AND status='open'""",
                    (session_id, user.username),
                )
                if cursor.rowcount == 0:
                    self.respond(404, {"error": "Không tìm thấy buổi học đang mở thuộc quyền giảng viên."})
                    return
            self.respond(200, {"session_id": session_id, "status": "closed"})
            return

        # GIẢNG VIÊN ĐIỂM DANH THỦ CÔNG / BÙ CHO SINH VIÊN
        if path.startswith("/api/sessions/") and path.endswith("/manual-checkin"):
            user = self.require_role("lecturer")
            if not user:
                return
            parts = path.split("/")
            try:
                session_id = int(parts[3])
            except (ValueError, IndexError):
                self.respond(400, {"error": "Mã buổi học không hợp lệ."})
                return
            student_raw = str(data.get("student_username", "")).strip()

            with connect() as db:
                st_user = find_user_in_db(db, student_raw)
                if not st_user:
                    self.respond(400, {"error": "Mã sinh viên không hợp lệ."})
                    return

                session = db.execute(
                    """SELECT s.id, s.status, c.id AS class_id FROM sessions s
                    JOIN classes c ON c.id=s.class_id
                    WHERE s.id=? AND c.lecturer_username=?""",
                    (session_id, user.username),
                ).fetchone()
                if not session:
                    self.respond(404, {"error": "Không tìm thấy buổi học thuộc quyền giảng viên."})
                    return

                st_keys = (student_raw, st_user["username"], st_user.get("alias") or student_raw)
                enrolled = db.execute(
                    """SELECT 1 FROM enrollments
                    WHERE class_id=? AND student_username IN (?, ?, ?)""",
                    (session["class_id"],) + st_keys,
                ).fetchone()
                if not enrolled:
                    self.respond(400, {"error": "Sinh viên chưa được ghi danh trong lớp học này."})
                    return

                # Record event under canonical username or student_raw
                try:
                    append_event(db, session_id, student_raw, user.username)
                except sqlite3.IntegrityError:
                    self.respond(409, {"error": "Sinh viên này đã được điểm danh trước đó."})
                    return

            self.respond(201, {
                "message": f"Đã điểm danh bù thành công cho sinh viên {st_user['name']} (MSV: {st_user['student_id']})!",
                "session_id": session_id,
                "student_username": student_raw,
            })
            return

        if path == "/api/attendance":
            user = self.require_role("student")
            if not user:
                return
            qr_token = str(data.get("qr_token", "")).strip()
            attendance_code = str(data.get("attendance_code", "")).strip()
            session_id = None
            if qr_token:
                try:
                    session_id = parse_qr_token(qr_token)
                except ValueError as error:
                    self.respond(400, {"error": str(error)})
                    return
            elif attendance_code:
                with connect() as db:
                    matched = db.execute(
                        """SELECT id FROM sessions
                        WHERE attendance_code=? AND status='open'""",
                        (attendance_code,),
                    ).fetchone()
                if matched:
                    session_id = matched["id"]
            if session_id is None:
                self.respond(400, {"error": "Mã điểm danh không đúng hoặc buổi học đã đóng."})
                return
            try:
                with connect() as db:
                    session = db.execute(
                        """SELECT s.id, s.status, c.id AS class_id FROM sessions s
                        JOIN classes c ON c.id=s.class_id WHERE s.id=?""",
                        (session_id,),
                    ).fetchone()
                    if not session or session["status"] != "open":
                        self.respond(400, {"error": "Buổi học không tồn tại hoặc đã đóng."})
                        return

                    st_keys = (user.effective_username, user.username, user.alias or user.effective_username)
                    enrolled = db.execute(
                        """SELECT 1 FROM enrollments
                        WHERE class_id=? AND student_username IN (?, ?, ?)""",
                        (session["class_id"],) + st_keys,
                    ).fetchone()
                    if not enrolled:
                        # Sinh viên thuộc danh sách lớp (VD CNTT2.K25) được điểm danh mọi buổi của lớp đó
                        enrolled = db.execute(
                            """SELECT 1 FROM users u JOIN classes c ON c.code=u.list_class_code
                            WHERE c.id=? AND (u.username=? OR u.alias=?)""",
                            (session["class_id"], user.username, user.alias or user.username),
                        ).fetchone()
                    if not enrolled:
                        self.respond(403, {"error": "Bạn chưa được ghi danh vào lớp này."})
                        return

                    append_event(db, session_id, user.effective_username, user.effective_username)
                self.respond(201, {"message": "Điểm danh thành công.", "session_id": session_id})
            except sqlite3.IntegrityError:
                self.respond(409, {"error": "Sinh viên đã điểm danh buổi học này."})
            return

        if path == "/api/blockchain/tamper":
            # Mô phỏng can thiệp sửa đổi trái phép hash
            if not self.require_demo_mode():
                return
            with connect() as db:
                tampered = tamper_last_event(db)
            if tampered is None:
                self.respond(400, {"error": "Chưa có lượt điểm danh nào để mô phỏng can thiệp."})
                return
            self.respond(200, {
                "message": f"Đã mô phỏng can thiệp sửa đổi trái phép hash tại Khối #{tampered}!",
                "tampered_block": tampered,
            })
            return

        if path == "/api/blockchain/repair":
            if not self.require_demo_mode():
                return
            # BƯỚC 1: Báo cáo điểm gãy chuỗi trước khi cho phép sửa
            if not bool(data.get("confirm")):
                with connect() as db:
                    report = inspect_chain(db)
                if report["chain_valid"]:
                    status, message = 409, "Chuỗi hiện đang toàn vẹn, không cần khôi phục."
                else:
                    status, message = 200, (
                        f"Phát hiện chuỗi bị sai lệch tại Khối #{report['broken_block']}. "
                        "Chưa có thay đổi nào được ghi. Gửi lại với confirm=true để ghi đè."
                    )
                self.respond(status, {**report, "message": message, "requires_confirmation": True, "applied": False})
                return
            # BƯỚC 2: Đã xác nhận — ghi lại toàn bộ chuỗi
            with connect() as db:
                rebuild_chain(db)
            self.respond(200, {
                "message": "Đã khôi phục tính toàn vẹn và sự đồng thuận của chuỗi Blockchain thành công!",
                "chain_valid": True,
                "applied": True,
            })
            return

        self.respond(404, {"error": "Không tìm thấy tài nguyên."})

    def do_PATCH(self) -> None:
        if urlparse(self.path).path.startswith("/api/attendance"):
            self.respond(405, {"error": "Lịch sử điểm danh chỉ được ghi nối tiếp, không thể sửa."})
        else:
            self.respond(404, {"error": "Không tìm thấy tài nguyên."})

    def do_DELETE(self) -> None:
        if urlparse(self.path).path.startswith("/api/attendance"):
            self.respond(405, {"error": "Lịch sử điểm danh chỉ được ghi nối tiếp, không thể xóa."})
        else:
            self.respond(404, {"error": "Không tìm thấy tài nguyên."})


def main() -> None:
    init_db()
    print("==================================================================")
    print("   HE THONG DIEM DANH SINH VIEN BLOCKCHAIN - TRUONG DH HAI PHONG")
    print(f"   Server dang chay tai: {BASE_URL} (dia chi {HOST}:{PORT})")
    print(f"   Du lieu: {DB_PATH}{' (tam thoi)' if EPHEMERAL_MODE else ''}")
    print("==================================================================")
    if HOST in ("0.0.0.0", "::"):
        share_url = BASE_URL if "localhost" not in BASE_URL and "127.0.0.1" not in BASE_URL else f"http://<IP-LAN>{':' + str(PORT) if PORT != 80 else ''}"
        print(f"   Gui link nay cho moi nguoi: {share_url}")
        print("   (May khac phai cung mang LAN/Wi-Fi va mo cong %d trong firewall)" % PORT)
        print("==================================================================")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()
