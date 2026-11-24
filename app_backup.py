from __future__ import annotations

import hashlib
import hmac
import binascii
import json
import os
import secrets
import sqlite3
import time
import base64
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import qrcode
import qrcode.image.svg


ROOT = Path(__file__).resolve().parent
DB_PATH = Path(os.environ.get("ATTENDANCE_DB", ROOT / "attendance.db"))
HOST = os.environ.get("ATTENDANCE_HOST", "127.0.0.1")
PORT = int(os.environ.get("ATTENDANCE_PORT", "8000"))
BASE_URL = os.environ.get("ATTENDANCE_BASE_URL", f"http://localhost:{PORT}")
SECRET_PATH = ROOT / ".demo-secret"
TOKEN_LIFETIME = 30 * 60
SESSIONS: dict[str, dict[str, str]] = {}
GENESIS_HASH = "0" * 64

DEMO_USERS = {
    "gv01": {"password": "GVdemo123!", "role": "lecturer", "name": "Nguyễn Minh Anh"},
    "sv01": {"password": "SVdemo123!", "role": "student", "name": "Trần Gia Bảo"},
    "sv02": {"password": "SVdemo123!", "role": "student", "name": "Lê Khánh Linh"},
}


def connect() -> sqlite3.Connection:
    class ClosingConnection(sqlite3.Connection):
        def __exit__(self, exc_type, exc_value, traceback):
            try:
                return super().__exit__(exc_type, exc_value, traceback)
            finally:
                self.close()

    db = sqlite3.connect(DB_PATH, factory=ClosingConnection)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA foreign_keys = ON")
    return db


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def get_secret() -> bytes:
    if not SECRET_PATH.exists():
        SECRET_PATH.write_bytes(secrets.token_bytes(32))
    return SECRET_PATH.read_bytes()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with connect() as db:
        db.executescript(
            """
            CREATE TABLE IF NOT EXISTS classes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                code TEXT NOT NULL UNIQUE,
                course_code TEXT NOT NULL DEFAULT '',
                name TEXT NOT NULL,
                lecturer_username TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS enrollments (
                class_id INTEGER NOT NULL REFERENCES classes(id),
                student_username TEXT NOT NULL,
                PRIMARY KEY (class_id, student_username)
            );
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
        class_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(classes)").fetchall()
        }
        if "course_code" not in class_columns:
            db.execute("ALTER TABLE classes ADD COLUMN course_code TEXT NOT NULL DEFAULT ''")
        db.execute(
            "UPDATE classes SET course_code=code WHERE course_code=''"
        )
        session_columns = {
            row["name"] for row in db.execute("PRAGMA table_info(sessions)").fetchall()
        }
        if "attendance_code" not in session_columns:
            db.execute("ALTER TABLE sessions ADD COLUMN attendance_code TEXT")
        db.execute(
            "CREATE UNIQUE INDEX IF NOT EXISTS sessions_attendance_code_unique "
            "ON sessions(attendance_code) WHERE attendance_code IS NOT NULL"
        )
        for row in db.execute(
            "SELECT id FROM sessions WHERE attendance_code IS NULL"
        ).fetchall():
            db.execute(
                "UPDATE sessions SET attendance_code=? WHERE id=?",
                (new_attendance_code(db), row["id"]),
            )
        db.execute(
            """INSERT OR IGNORE INTO classes(code, course_code, name, lecturer_username)
            VALUES(?, ?, ?, ?)""",
            ("CNTT101", "CNTT101", "Nhập môn Công nghệ thông tin", "gv01"),
        )
        class_id = db.execute(
            "SELECT id FROM classes WHERE code = ?", ("CNTT101",)
        ).fetchone()["id"]
        for username in ("sv01", "sv02"):
            db.execute(
                "INSERT OR IGNORE INTO enrollments(class_id, student_username) VALUES(?, ?)",
                (class_id, username),
            )
        if not db.execute("SELECT 1 FROM sessions WHERE class_id = ?", (class_id,)).fetchone():
            db.execute(
                """INSERT INTO sessions(
                    class_id, title, starts_at, ends_at, attendance_code
                ) VALUES(?, ?, ?, ?, ?)""",
                (
                    class_id,
                    "Buổi demo điểm danh",
                    now_iso(),
                    "2099-12-31T23:59:59+00:00",
                    new_attendance_code(db),
                ),
            )


def new_attendance_code(db: sqlite3.Connection) -> str:
    while True:
        code = f"{secrets.randbelow(1_000_000):06d}"
        if not db.execute(
            "SELECT 1 FROM sessions WHERE attendance_code = ?", (code,)
        ).fetchone():
            return code


def event_digest(event: dict[str, object]) -> str:
    canonical = json.dumps(event, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def append_event(
    db: sqlite3.Connection, session_id: int, student_username: str, actor_username: str
) -> None:
    previous = db.execute(
        "SELECT event_hash FROM attendance_events ORDER BY id DESC LIMIT 1"
    ).fetchone()
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


def qr_svg(value: str) -> bytes:
    image = qrcode.make(
        value,
        image_factory=qrcode.image.svg.SvgPathImage,
        box_size=8,
        border=4,
    )
    from io import BytesIO

    output = BytesIO()
    image.save(output)
    return output.getvalue()


HTML = r"""<!doctype html>
<html lang="vi"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width">
<title>Điểm danh Blockchain - Demo</title>
<style>
body{font:16px system-ui,sans-serif;max-width:980px;margin:32px auto;padding:0 16px;color:#172033;background:#f4f7fb}
h1{color:#124e78}section{background:white;padding:18px;margin:14px 0;border-radius:12px;box-shadow:0 2px 8px #0001}
input,select,button{padding:9px;margin:4px;border:1px solid #ccd5e0;border-radius:6px}button{background:#126e82;color:white;cursor:pointer}
pre{white-space:pre-wrap;background:#f3f5f7;padding:12px;border-radius:6px}.muted{color:#556}
#qr{max-width:260px} .row{display:flex;flex-wrap:wrap;gap:8px}
.table-wrap{overflow-x:auto;margin:12px 0}table{width:100%;border-collapse:collapse;background:white}
th,td{text-align:left;padding:10px 12px;border-bottom:1px solid #e1e7ed}th{background:#eaf1f6;color:#124e78}
.status-present{color:#18743a;font-weight:700}.status-absent{color:#b42318;font-weight:700}
.status-pending,.status-open{color:#875d00;font-weight:700}.empty{color:#667085;padding:12px}
</style></head><body>
<h1>Hệ thống điểm danh Blockchain</h1>
<p>Demo học tập: QR có chữ ký, phân quyền và nhật ký điểm danh liên kết SHA-256.</p>
<section><h2>1. Đăng nhập</h2>
<input id="username" placeholder="Tên đăng nhập" value="gv01">
<input id="password" placeholder="Mật khẩu" type="password" value="GVdemo123!">
<button onclick="login()">Đăng nhập</button><button onclick="logout()">Đăng xuất</button>
<p id="who"></p><p class="muted">Giảng viên: gv01 / GVdemo123! · Sinh viên: sv01 hoặc sv02 / SVdemo123!</p></section>
<section id="lecturerPanel" hidden><h2>Khu vực giảng viên</h2>
<h3>Tạo môn học / lớp</h3>
<div class="row"><input id="courseCode" placeholder="Mã môn học"><input id="classCode" placeholder="Mã lớp"><input id="className" placeholder="Tên môn học"><button onclick="createClass()">Tạo lớp</button></div>
<h3>Chọn lớp và ghi danh</h3>
<div class="row"><select id="classes" onchange="showSelectedClass()"></select><span id="selectedClass"></span><input id="studentUsername" placeholder="Tên sinh viên (sv01/sv02)"><button onclick="enrollStudent()">Ghi danh</button></div>
<h3>Tạo buổi điểm danh</h3>
<div class="row"><input id="sessionTitle" placeholder="Tên buổi học"><button onclick="createSession()">Tạo mã điểm danh + QR</button><input id="sessionId" placeholder="Mã buổi cần đóng"><button onclick="closeSession()">Đóng buổi</button><button onclick="loadSessions()">Tải buổi học</button></div>
<p id="attendanceCodeDisplay"></p><img id="qr"><p id="qrLink"></p>
<h3>Kiểm tra toàn bộ lịch sử</h3><button onclick="verify()">Kiểm tra chuỗi băm</button><pre id="audit"></pre>
<p class="muted">Giảng viên được quyền tạo lớp/buổi, ghi danh sinh viên, phát QR, đóng buổi và kiểm tra toàn vẹn lịch sử.</p></section>
<section id="studentPanel" hidden><h2>Khu vực sinh viên</h2>
<p>Quét QR hoặc nhập mã điểm danh do giảng viên cung cấp, sau đó bấm xác nhận.</p>
<input id="qrToken" placeholder="Mã QR hoặc mã điểm danh 6 số" style="width:min(90%,650px)">
<button onclick="checkIn()">Xác nhận điểm danh</button><button onclick="loadMine()">Xem điểm danh của tôi</button>
<div id="mine"></div>
<p class="muted">Sinh viên chỉ xem lớp đã tham gia, điểm danh bằng QR và xem lịch sử cá nhân; không thể tạo lớp/buổi hoặc xem audit toàn cục.</p></section>
<section id="classPanel" hidden><h2>Môn học và thống kê điểm danh</h2>
<div id="classList"></div><h3>Chi tiết từng buổi</h3><button onclick="loadSessions()">Tải danh sách buổi học</button><div id="sessions"></div></section>
<pre id="msg"></pre>
<script>
let auth=localStorage.getItem('attendance-token')||'';
let role='';
const code=new URLSearchParams(location.search).get('code'); if(code) document.querySelector('#qrToken').value=code;
async function api(path,method='GET',body){let r=await fetch(path,{method,headers:{'Content-Type':'application/json',...(auth?{Authorization:'Bearer '+auth}:{})},...(body?{body:JSON.stringify(body)}:{})});let d=await r.json();if(!r.ok)throw Error(d.error||r.statusText);return d}
function show(x){document.querySelector('#msg').textContent=typeof x==='string'?x:JSON.stringify(x,null,2)}
function setRole(user){role=user.role;who.textContent='Đã đăng nhập: '+user.name+' — '+(role==='lecturer'?'Giảng viên':'Sinh viên');lecturerPanel.hidden=role!=='lecturer';studentPanel.hidden=role!=='student';classPanel.hidden=false;}
function renderTable(target,headers,rows,emptyMessage){const box=document.querySelector(target);box.replaceChildren();if(!rows.length){const p=document.createElement('p');p.className='empty';p.textContent=emptyMessage;box.append(p);return;}const wrap=document.createElement('div');wrap.className='table-wrap';const table=document.createElement('table');const thead=document.createElement('thead');const headerRow=document.createElement('tr');headers.forEach(text=>{const th=document.createElement('th');th.textContent=text;headerRow.append(th)});thead.append(headerRow);const tbody=document.createElement('tbody');rows.forEach(row=>{const tr=document.createElement('tr');row.forEach(cell=>{const td=document.createElement('td');if(cell&&typeof cell==='object'){td.textContent=cell.text;td.className=cell.className||''}else{td.textContent=cell??'—'}tr.append(td)});tbody.append(tr)});table.append(thead,tbody);wrap.append(table);box.append(wrap)}
function attendanceLabel(status){if(status==='present')return{text:'Đã điểm danh',className:'status-present'};if(status==='absent')return{text:'Vắng',className:'status-absent'};return{text:'Chưa điểm danh',className:'status-pending'}}
function renderSessions(classes,rows){const summary=classes.map(c=>{const meetings=rows.filter(s=>s.class_id===c.id);if(role==='lecturer'){const attended=meetings.reduce((n,s)=>n+s.attended_count,0);const absent=meetings.filter(s=>s.status==='closed').reduce((n,s)=>n+s.enrolled_count-s.attended_count,0);const open=meetings.filter(s=>s.status==='open').length;return[c.name,c.code,meetings.length,attended,absent,open]}const attended=meetings.filter(s=>s.attendance_status==='present').length;const absent=meetings.filter(s=>s.attendance_status==='absent').length;const pending=meetings.filter(s=>s.attendance_status==='pending').length;return[c.name,c.code,meetings.length,attended,absent,pending]});const summaryHeaders=['Môn học','Mã lớp','Số buổi','Lượt đã điểm danh','Lượt vắng',role==='lecturer'?'Buổi đang mở':'Chưa điểm danh'];renderTable('#classList',summaryHeaders,summary,'Bạn chưa được ghi danh vào môn học nào.');if(role==='lecturer'){renderTable('#sessions',['Môn học','Buổi học','Mã điểm danh','Trạng thái buổi','Có mặt','Vắng'],rows.map(s=>[s.class_name,s.title,s.attendance_code,s.status==='closed'?'Đã đóng':{text:'Đang mở',className:'status-open'},`${s.attended_count}/${s.enrolled_count}`,s.status==='closed'?s.enrolled_count-s.attended_count:'—']),'Chưa có buổi học.')}else{renderTable('#sessions',['Môn học','Buổi học','Trạng thái buổi','Điểm danh'],rows.map(s=>[s.class_name,s.title,s.status==='closed'?'Đã đóng':{text:'Đang mở',className:'status-open'},attendanceLabel(s.attendance_status)]),'Chưa có buổi học trong các môn của bạn.')}
}
async function login(){try{let d=await api('/api/login','POST',{username:username.value,password:password.value});auth=d.token;localStorage.setItem('attendance-token',auth);setRole(d);await loadClasses();await loadSessions();show('Đăng nhập thành công')}catch(e){show(e.message)}}
function logout(){auth='';role='';localStorage.removeItem('attendance-token');who.textContent='Đã đăng xuất';lecturerPanel.hidden=true;studentPanel.hidden=true;classPanel.hidden=true;show('')}
async function showSelectedClass(){const selected=document.querySelector('#classes').selectedOptions[0];document.querySelector('#selectedClass').textContent=selected?`Mã môn: ${selected.dataset.courseCode||'—'} | Mã lớp: ${selected.dataset.classCode||'—'} | ${selected.dataset.className||''}`:''}
async function loadClasses(){try{let d=await api('/api/classes');const select=document.querySelector('#classes');select.replaceChildren(...d.classes.map(c=>{let o=document.createElement('option');o.value=c.id;o.dataset.courseCode=c.course_code;o.dataset.classCode=c.code;o.dataset.className=c.name;o.textContent=`${c.course_code||'Chưa có mã môn'} — ${c.code} — ${c.name}`;return o;}));showSelectedClass();renderTable('#classList',['Mã môn học','Mã lớp','Tên môn học'],d.classes.map(c=>[c.course_code||'—',c.code,c.name]),'Bạn chưa có lớp nào.')}catch(e){show(e.message)}}
async function createClass(){try{await api('/api/classes','POST',{course_code:document.querySelector('#courseCode').value,code:document.querySelector('#classCode').value,name:document.querySelector('#className').value});await loadClasses();await loadSessions();show('Đã tạo môn học và lớp')}catch(e){show(e.message)}}
async function enrollStudent(){try{await api('/api/classes/'+document.querySelector('#classes').value+'/enrollments','POST',{student_username:document.querySelector('#studentUsername').value});show('Đã ghi danh sinh viên')}catch(e){show(e.message)}}
async function createSession(){try{let d=await api('/api/sessions','POST',{class_id:+document.querySelector('#classes').value,title:document.querySelector('#sessionTitle').value});document.querySelector('#qr').src='/api/sessions/'+d.id+'/qr.svg?token='+encodeURIComponent(d.qr_token);document.querySelector('#qrLink').textContent=d.qr_url;document.querySelector('#attendanceCodeDisplay').textContent='Mã điểm danh buổi '+d.title+': '+d.attendance_code;await loadSessions();show('Đã tạo buổi học, mã điểm danh và QR')}catch(e){show(e.message)}}
async function closeSession(){try{await api('/api/sessions/'+document.querySelector('#sessionId').value+'/close','POST',{});await loadSessions();show('Đã đóng buổi học')}catch(e){show(e.message)}}
async function loadSessions(){try{let [classesData,sessionsData]=await Promise.all([api('/api/classes'),api('/api/sessions')]);renderSessions(classesData.classes,sessionsData.sessions)}catch(e){show(e.message)}}
async function checkIn(){try{const input=document.querySelector('#qrToken').value.trim();const payload=/^\d{6}$/.test(input)?{attendance_code:input}:{qr_token:input};let d=await api('/api/attendance','POST',payload);show(d.message);await loadSessions();await loadMine()}catch(e){show(e.message)}}
async function loadMine(){try{let d=await api('/api/attendance/mine');renderTable('#mine',['Lớp','Buổi học','Thời gian điểm danh'],d.records.map(r=>[r.class_code,r.title,new Date(r.occurred_at).toLocaleString('vi-VN')]),'Bạn chưa có lượt điểm danh nào.')}catch(e){show(e.message)}}
async function verify(){try{let d=await api('/api/audit/verify');document.querySelector('#audit').textContent=JSON.stringify(d,null,2)}catch(e){show(e.message)}}
if(auth){api('/api/me').then(d=>{setRole(d);loadClasses();loadSessions()}).catch(logout)}
if(code) show('Đã nhận mã QR. Đăng nhập tài khoản sinh viên để điểm danh.');
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    server_version = "AttendanceDemo/1.0"

    def log_message(self, fmt: str, *args: object) -> None:
        target = self.requestline.split(" ")[1]
        message = (fmt % args).replace(target, urlparse(target).path)
        print(f"{self.address_string()} - {message}")

    def respond(self, status: int, data: object, content_type: str = "application/json") -> None:
        body = data if isinstance(data, bytes) else json.dumps(
            data, ensure_ascii=False
        ).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type + ("; charset=utf-8" if content_type.startswith("text/") or content_type == "application/json" else ""))
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, object]:
        length = int(self.headers.get("Content-Length", "0"))
        if length > 16_384:
            raise ValueError("Nội dung gửi lên quá lớn.")
        payload = json.loads(self.rfile.read(length) or b"{}")
        if not isinstance(payload, dict):
            raise ValueError("Dữ liệu gửi lên phải là JSON object.")
        return payload

    def current_user(self) -> tuple[str, dict[str, str]] | None:
        token = self.headers.get("Authorization", "").removeprefix("Bearer ")
        username = SESSIONS.get(token, {}).get("username")
        return (username, DEMO_USERS[username]) if username in DEMO_USERS else None

    def require_role(self, role: str) -> tuple[str, dict[str, str]] | None:
        user = self.current_user()
        if user is None:
            self.respond(401, {"error": "Vui lòng đăng nhập."})
        elif user[1]["role"] != role:
            self.respond(403, {"error": "Bạn không có quyền thực hiện thao tác này."})
            return None
        return user

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
                self.respond(200, {"username": user[0], **user[1]})
            return
        if path == "/api/classes":
            user = self.current_user()
            if not user:
                self.respond(401, {"error": "Vui lòng đăng nhập."})
                return
            if user[1]["role"] not in ("lecturer", "student"):
                self.respond(403, {"error": "Bạn không có quyền thực hiện thao tác này."})
                return
            with connect() as db:
                if user[1]["role"] == "lecturer":
                    rows = db.execute(
                        """SELECT id, code, course_code, name FROM classes
                        WHERE lecturer_username = ? ORDER BY id""",
                        (user[0],),
                    ).fetchall()
                else:
                    rows = db.execute(
                        """SELECT c.id, c.code, c.course_code, c.name FROM classes c
                        JOIN enrollments e ON e.class_id = c.id
                        WHERE e.student_username = ? ORDER BY c.id""",
                        (user[0],),
                    ).fetchall()
            self.respond(200, {"classes": [dict(row) for row in rows]})
            return
        if path == "/api/sessions":
            user = self.current_user()
            if not user:
                self.respond(401, {"error": "Vui lòng đăng nhập."})
                return
            with connect() as db:
                query = """SELECT s.id, s.class_id, c.code AS class_code,
                    c.course_code, c.name AS class_name, s.title,
                    s.starts_at, s.ends_at, s.status FROM sessions s
                    JOIN classes c ON c.id = s.class_id"""
                params: tuple[object, ...] = ()
                if user[1]["role"] == "lecturer":
                    query = """SELECT s.id, s.class_id, c.code AS class_code,
                        c.course_code, c.name AS class_name, s.title, s.attendance_code,
                        s.starts_at, s.ends_at, s.status,
                        (SELECT COUNT(*) FROM attendance_events a WHERE a.session_id=s.id)
                            AS attended_count,
                        (SELECT COUNT(*) FROM enrollments e WHERE e.class_id=c.id)
                            AS enrolled_count
                        FROM sessions s JOIN classes c ON c.id=s.class_id
                        WHERE c.lecturer_username = ?"""
                    params = (user[0],)
                else:
                    query = """SELECT s.id, s.class_id, c.code AS class_code,
                        c.course_code, c.name AS class_name, s.title,
                        s.starts_at, s.ends_at, s.status,
                        CASE WHEN EXISTS (
                            SELECT 1 FROM attendance_events a
                            WHERE a.session_id=s.id AND a.student_username=?
                        ) THEN 'present'
                        WHEN s.status='closed' THEN 'absent'
                        ELSE 'pending' END AS attendance_status
                        FROM sessions s JOIN classes c ON c.id=s.class_id
                        JOIN enrollments e ON e.class_id=c.id
                        WHERE e.student_username = ?"""
                    params = (user[0], user[0])
                rows = db.execute(query + " ORDER BY s.id DESC", params).fetchall()
            self.respond(200, {"sessions": [dict(row) for row in rows]})
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
                        (session_id, user[0]),
                    ).fetchone()
                if not owned or parse_qr_token(token) != session_id:
                    raise ValueError
                self.respond(200, qr_svg(f"{BASE_URL}/?code={token}"), "image/svg+xml")
            except (ValueError, KeyError, IndexError):
                self.respond(400, {"error": "Không thể tạo mã QR cho buổi học này."})
            return
        if path == "/api/attendance/mine":
            user = self.require_role("student")
            if not user:
                return
            with connect() as db:
                rows = db.execute(
                    """SELECT a.session_id, s.title, c.code AS class_code,
                    a.occurred_at, a.event_hash FROM attendance_events a
                    JOIN sessions s ON s.id=a.session_id JOIN classes c ON c.id=s.class_id
                    WHERE a.student_username=? ORDER BY a.id DESC""",
                    (user[0],),
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
        self.respond(404, {"error": "Không tìm thấy tài nguyên."})

    def do_POST(self) -> None:
        try:
            data = self.read_json()
        except (ValueError, json.JSONDecodeError) as error:
            self.respond(400, {"error": str(error)})
            return
        path = urlparse(self.path).path
        if path == "/api/login":
            username, password = data.get("username"), data.get("password")
            user = DEMO_USERS.get(str(username))
            if not user or not hmac.compare_digest(str(password), user["password"]):
                self.respond(401, {"error": "Tên đăng nhập hoặc mật khẩu không đúng."})
                return
            token = secrets.token_urlsafe(32)
            SESSIONS[token] = {"username": str(username)}
            self.respond(200, {"token": token, "username": username, **user})
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
                        (code, course_code, name, user[0]),
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
            student = str(data.get("student_username", "")).strip()
            if not student or student not in DEMO_USERS or DEMO_USERS[student]["role"] != "student":
                self.respond(400, {"error": "Tài khoản sinh viên không hợp lệ."})
                return
            with connect() as db:
                owned = db.execute(
                    "SELECT 1 FROM classes WHERE id=? AND lecturer_username=?",
                    (class_id, user[0]),
                ).fetchone()
                if not owned:
                    self.respond(404, {"error": "Không tìm thấy lớp thuộc quyền giảng viên."})
                    return
                try:
                    db.execute(
                        "INSERT INTO enrollments(class_id, student_username) VALUES(?, ?)",
                        (class_id, student),
                    )
                except sqlite3.IntegrityError:
                    self.respond(409, {"error": "Sinh viên đã được ghi danh vào lớp."})
                    return
            self.respond(201, {"class_id": class_id, "student_username": student})
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
                    (class_id, user[0]),
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
                    "qr_url": f"{BASE_URL}/?code={token}",
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
                    (session_id, user[0]),
                )
                if cursor.rowcount == 0:
                    self.respond(404, {"error": "Không tìm thấy buổi học đang mở thuộc quyền giảng viên."})
                    return
            self.respond(200, {"session_id": session_id, "status": "closed"})
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
                    enrolled = db.execute(
                        "SELECT 1 FROM enrollments WHERE class_id=? AND student_username=?",
                        (session["class_id"], user[0]),
                    ).fetchone()
                    if not enrolled:
                        self.respond(403, {"error": "Bạn chưa được ghi danh vào lớp này."})
                        return
                    append_event(db, session_id, user[0], user[0])
                self.respond(201, {"message": "Điểm danh thành công.", "session_id": session_id})
            except sqlite3.IntegrityError:
                self.respond(409, {"error": "Sinh viên đã điểm danh buổi học này."})
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


if __name__ == "__main__":
    init_db()
    print(f"Demo available at {BASE_URL} (bind {HOST}:{PORT})")
    ThreadingHTTPServer((HOST, PORT), Handler).serve_forever()
