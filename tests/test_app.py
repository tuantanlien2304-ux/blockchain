import base64
import io
import json
import sqlite3
import tempfile
import threading
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import app


class AttendanceDemoTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        app.DB_PATH = root / "test.db"
        app.SECRET_PATH = root / ".demo-secret"
        app.BASE_URL = "http://127.0.0.1"
        app.DEMO_MODE = True
        app.SESSIONS.clear()
        app.init_db()
        self.server = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=2)
        self.temp.cleanup()

    def demo_mode_context(self, enabled):
        class DemoMode:
            def __enter__(_self):
                _self.saved = app.DEMO_MODE
                app.DEMO_MODE = enabled

            def __exit__(_self, *exc):
                app.DEMO_MODE = _self.saved
                return False

        return DemoMode()

    def check_in_once(self, teacher, student):
        session = self.request(
            "POST",
            "/api/sessions",
            {"class_id": 1, "title": "Buổi kiểm thử chuỗi"},
            teacher,
            expected=201,
        )
        self.request(
            "POST",
            "/api/attendance",
            {"qr_token": session["qr_token"]},
            student,
            expected=201,
        )
        return session

    def request(self, method, path, body=None, token=None, expected=200):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urlopen(request) as response:
                status, content = response.status, response.read()
        except HTTPError as error:
            status, content = error.code, error.read()
            error.close()
        self.assertEqual(status, expected, content.decode("utf-8"))
        if ".svg" in path:
            return content
        return json.loads(content)

    def login(self, username, password):
        return self.request(
            "POST", "/api/login", {"username": username, "password": password}
        )["token"]

    def test_role_permissions_and_class_session_qr(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv01", "SVdemo123!")
        self.request(
            "POST",
            "/api/classes",
            {"code": "TEST101", "name": "Lớp kiểm thử"},
            student,
            expected=403,
        )
        class_data = self.request(
            "POST",
            "/api/classes",
            {
                "course_code": "CS101",
                "code": "TEST101",
                "name": "Lớp kiểm thử",
            },
            teacher,
            expected=201,
        )
        self.assertEqual("CS101", class_data["course_code"])
        session = self.request(
            "POST",
            "/api/sessions",
            {"class_id": class_data["id"], "title": "Buổi kiểm thử"},
            teacher,
            expected=201,
        )
        lecturer_sessions = self.request("GET", "/api/sessions", token=teacher)["sessions"]
        self.assertEqual(0, lecturer_sessions[0]["attended_count"])
        self.assertEqual(0, lecturer_sessions[0]["enrolled_count"])
        self.assertEqual("Lớp kiểm thử", lecturer_sessions[0]["class_name"])
        self.assertEqual("CS101", lecturer_sessions[0]["course_code"])
        self.assertRegex(session["attendance_code"], r"^\d{6}$")
        image = self.request(
            "GET",
            f"/api/sessions/{session['id']}/qr.svg?token={session['qr_token']}",
            token=teacher,
        )
        self.assertIn(b"<svg", image)
        # Dọn dẹp: xoá lớp/buổi do chính test này tạo để dữ liệu demo không bị lẫn lớp kiểm thử
        with app.connect() as db:
            db.execute("DELETE FROM enrollments WHERE class_id = ?", (class_data["id"],))
            db.execute("DELETE FROM attendance_events WHERE session_id = ?", (session["id"],))
            db.execute("DELETE FROM sessions WHERE id = ?", (session["id"],))
            db.execute("DELETE FROM classes WHERE id = ?", (class_data["id"],))

    def test_attendance_is_single_use_append_only_and_auditable(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv01", "SVdemo123!")
        session = self.request(
            "GET", "/api/sessions", token=student
        )["sessions"][0]
        self.assertEqual("pending", session["attendance_status"])
        self.assertTrue(session["class_name"])
        self.assertNotIn("attendance_code", session)
        with app.connect() as db:
            code = db.execute(
                "SELECT attendance_code FROM sessions WHERE id=?",
                (session["id"],),
            ).fetchone()["attendance_code"]
        self.assertRegex(code, r"^\d{6}$")
        self.request(
            "POST",
            "/api/attendance",
            {"attendance_code": code},
            student,
            expected=201,
        )
        session = self.request("GET", "/api/sessions", token=student)["sessions"][0]
        self.assertEqual("present", session["attendance_status"])
        self.request(
            "POST",
            "/api/attendance",
            {"attendance_code": code},
            student,
            expected=409,
        )
        result = self.request("GET", "/api/audit/verify", token=teacher)
        self.assertEqual({"valid": True, "events": 1, "broken_at": None}, result)
        with app.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE attendance_events SET student_username='sv02' WHERE id=1")
            db.execute("DROP TRIGGER attendance_no_update")
            db.execute("UPDATE attendance_events SET student_username='sv02' WHERE id=1")
        result = self.request("GET", "/api/audit/verify", token=teacher)
        self.assertFalse(result["valid"])
        self.assertEqual(1, result["broken_at"])

    def test_invalid_qr_and_student_cannot_read_global_audit(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv02", "SVdemo123!")
        with urlopen(self.base + "/") as response:
            page = response.read().decode("utf-8")
        self.assertIn('id="dashboard" class="hidden"', page)
        self.assertIn('id="qrScanButton"', page)
        self.assertIn("async function startQrScanner()", page)
        self.assertIn("scanQrImage(event)", page)
        self.assertIn('id="qrImageInput"', page)
        self.assertNotIn('value="gv01"', page)
        self.request(
            "POST",
            "/api/attendance",
            {"qr_token": "forged.payload"},
            student,
            expected=400,
        )
        self.request("GET", "/api/audit/verify", token=student, expected=403)
        self.request("GET", "/api/blockchain/explorer", token=student, expected=403)
        self.request("GET", "/api/blockchain/explorer", expected=401)
        with app.connect() as db:
            session = db.execute("SELECT id FROM sessions LIMIT 1").fetchone()["id"]
        valid = app.make_qr_token(session)
        encoded, signature = valid.split(".")
        tampered = ("A" if encoded[0] != "A" else "B") + encoded[1:] + "." + signature
        self.request(
            "POST",
            "/api/attendance",
            {"qr_token": tampered},
            student,
            expected=400,
        )
        self.assertTrue(teacher)

    def test_student_can_decode_qr_from_image_without_camera(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv01", "SVdemo123!")
        self.request("POST", "/api/attendance/scan-image", {}, expected=401)
        self.request(
            "POST", "/api/attendance/scan-image", {}, token=teacher, expected=403
        )
        self.request(
            "POST",
            "/api/attendance/scan-image",
            {"image": "data:image/png;base64,not-a-valid-image"},
            token=student,
            expected=400,
        )
        session = self.request("GET", "/api/sessions", token=student)["sessions"][0]
        token = app.make_qr_token(session["id"])
        image = io.BytesIO()
        app.qrcode.make(f"{app.BASE_URL}/?code={token}").save(image, format="PNG")
        image_data = "data:image/png;base64," + base64.b64encode(
            image.getvalue()
        ).decode("ascii")

        decoded = self.request(
            "POST",
            "/api/attendance/scan-image",
            {"image": image_data},
            token=student,
        )
        self.assertEqual(token, decoded["qr_token"])
        self.request(
            "POST",
            "/api/attendance",
            {"qr_token": decoded["qr_token"]},
            token=student,
            expected=201,
        )

    def test_teacher_can_create_multiple_sessions(self):
        teacher = self.login("gv01", "GVdemo123!")
        created = [
            self.request(
                "POST",
                "/api/sessions",
                {"class_id": 1, "title": title},
                teacher,
                expected=201,
            )
            for title in ("Buổi bổ sung A", "Buổi bổ sung B")
        ]
        sessions = self.request("GET", "/api/sessions", token=teacher)["sessions"]
        session_ids = {session["id"] for session in sessions}
        self.assertNotEqual(created[0]["id"], created[1]["id"])
        self.assertTrue(all(session["id"] in session_ids for session in created))

    def test_student_session_list_deduplicates_alias_enrollments(self):
        student = self.login("sv01", "SVdemo123!")
        sessions = self.request("GET", "/api/sessions", token=student)["sessions"]
        session_ids = [session["id"] for session in sessions]
        self.assertEqual(len(session_ids), len(set(session_ids)))

    def test_enrollment_and_session_closing(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv02", "SVdemo123!")
        class_data = self.request(
            "POST",
            "/api/classes",
            {
                "course_code": "CS-CLOSE",
                "code": "CLOSE101",
                "name": "Lớp đóng buổi",
            },
            teacher,
            expected=201,
        )
        self.request(
            "POST",
            f"/api/classes/{class_data['id']}/enrollments",
            {"student_username": "sv02"},
            teacher,
            expected=201,
        )
        session = self.request(
            "POST",
            "/api/sessions",
            {"class_id": class_data["id"], "title": "Buổi có trạng thái"},
            teacher,
            expected=201,
        )
        self.request(
            "POST",
            f"/api/sessions/{session['id']}/close",
            {},
            teacher,
            expected=200,
        )
        student_sessions = self.request("GET", "/api/sessions", token=student)["sessions"]
        closed_session = next(row for row in student_sessions if row["id"] == session["id"])
        self.assertEqual("absent", closed_session["attendance_status"])
        self.request(
            "POST",
            "/api/attendance",
            {"qr_token": session["qr_token"]},
            student,
            expected=400,
        )

    def test_blockchain_explorer_tamper_and_repair(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv01", "SVdemo123!")

        # 1. Check initial blockchain state
        explorer = self.request("GET", "/api/blockchain/explorer", token=teacher)
        self.assertTrue(explorer["valid"])
        self.assertGreaterEqual(explorer["total_blocks"], 1) # Has at least Genesis

        # 2. Student checks in
        session = self.request(
            "POST",
            "/api/sessions",
            {"class_id": 1, "title": "Buoi Blockchain Test"},
            teacher,
            expected=201,
        )
        self.request(
            "POST",
            "/api/attendance",
            {"qr_token": session["qr_token"]},
            student,
            expected=201,
        )

        # 3. Explorer should show new block
        explorer_after = self.request("GET", "/api/blockchain/explorer", token=teacher)
        self.assertTrue(explorer_after["valid"])
        self.assertEqual(explorer_after["total_attendance_events"], 1)

        # 4. Simulate malicious tampering
        tamper_res = self.request("POST", "/api/blockchain/tamper", {}, token=teacher, expected=200)
        self.assertIn("tampered_block", tamper_res)

        # 5. Explorer must detect tampering!
        explorer_tampered = self.request("GET", "/api/blockchain/explorer", token=teacher)
        self.assertFalse(explorer_tampered["valid"])
        self.assertIsNotNone(explorer_tampered["broken_at"])

        # 6. Repair must report the break without rewriting anything
        report = self.request("POST", "/api/blockchain/repair", {}, token=teacher, expected=200)
        self.assertFalse(report["applied"])
        self.assertTrue(report["requires_confirmation"])
        self.assertFalse(report["chain_valid"])
        self.assertEqual(tamper_res["tampered_block"], report["broken_block"])
        self.assertEqual(1, len(report["details"]))
        self.assertEqual(tamper_res["tampered_block"], report["details"][0]["block"])
        explorer_still_broken = self.request("GET", "/api/blockchain/explorer", token=teacher)
        self.assertFalse(explorer_still_broken["valid"])

        # 7. Repair with explicit confirmation
        confirmed = self.request(
            "POST", "/api/blockchain/repair", {"confirm": True}, token=teacher, expected=200
        )
        self.assertTrue(confirmed["applied"])
        self.assertTrue(confirmed["chain_valid"])
        explorer_repaired = self.request("GET", "/api/blockchain/explorer", token=teacher)
        self.assertTrue(explorer_repaired["valid"])
        self.assertIsNone(explorer_repaired["broken_at"])

    def test_tamper_and_repair_without_demo_flag_are_hidden(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv01", "SVdemo123!")
        self.check_in_once(teacher, student)

        with self.demo_mode_context(False):
            self.request("POST", "/api/blockchain/tamper", {}, token=teacher, expected=404)
            self.request("POST", "/api/blockchain/repair", {"confirm": True}, token=teacher, expected=404)

        # Chain untouched by the blocked calls
        explorer = self.request("GET", "/api/blockchain/explorer", token=teacher)
        self.assertTrue(explorer["valid"])
        self.assertIsNone(explorer["broken_at"])

    def test_tamper_and_repair_require_lecturer(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv01", "SVdemo123!")
        self.check_in_once(teacher, student)

        self.request("POST", "/api/blockchain/tamper", {}, token=student, expected=403)
        self.request("POST", "/api/blockchain/repair", {}, token=student, expected=403)
        self.request("POST", "/api/blockchain/tamper", {}, expected=401)
        self.request("POST", "/api/blockchain/repair", {"confirm": True}, expected=401)

        explorer = self.request("GET", "/api/blockchain/explorer", token=teacher)
        self.assertTrue(explorer["valid"])

    def test_tamper_then_repair_restores_valid_chain(self):
        teacher = self.login("gv01", "GVdemo123!")
        student = self.login("sv01", "SVdemo123!")
        session = self.check_in_once(teacher, student)

        self.request("POST", "/api/blockchain/tamper", {}, token=teacher, expected=200)
        verify = self.request("GET", "/api/audit/verify", token=teacher)
        self.assertFalse(verify["valid"])
        self.assertIsNotNone(verify["broken_at"])

        # Repair is a no-op until confirmed
        report = self.request("POST", "/api/blockchain/repair", {}, token=teacher, expected=200)
        self.assertFalse(report["applied"])
        self.assertEqual(verify["broken_at"], report["broken_block"])
        self.assertFalse(self.request("GET", "/api/audit/verify", token=teacher)["valid"])

        # Confirmed repair restores the chain without losing events
        confirmed = self.request(
            "POST", "/api/blockchain/repair", {"confirm": True}, token=teacher, expected=200
        )
        self.assertTrue(confirmed["applied"])
        self.assertTrue(confirmed["chain_valid"])
        verify_after = self.request("GET", "/api/audit/verify", token=teacher)
        self.assertEqual({"valid": True, "events": 1, "broken_at": None}, verify_after)

        # Append-only triggers are back in place after repair
        with app.connect() as db:
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("UPDATE attendance_events SET event_hash='x' WHERE id=1")
            with self.assertRaises(sqlite3.IntegrityError):
                db.execute("DELETE FROM attendance_events WHERE id=1")

        # A later check-in still chains onto the repaired ledger
        student2 = self.login("sv02", "SVdemo123!")
        self.request(
            "POST", "/api/attendance", {"qr_token": session["qr_token"]}, student2, expected=201
        )
        final = self.request("GET", "/api/audit/verify", token=teacher)
        self.assertEqual({"valid": True, "events": 2, "broken_at": None}, final)

    def test_repair_on_valid_chain_reports_nothing_to_fix(self):
        teacher = self.login("gv01", "GVdemo123!")
        self.request("POST", "/api/blockchain/repair", {}, token=teacher, expected=409)
        self.request("POST", "/api/blockchain/repair", {"confirm": True}, token=teacher, expected=200)
        explorer = self.request("GET", "/api/blockchain/explorer", token=teacher)
        self.assertTrue(explorer["valid"])

    def test_lecturer_views_attendees_and_manual_checkin(self):
        teacher = self.login("gv01", "GVdemo123!")
        session = self.request(
            "POST",
            "/api/sessions",
            {"class_id": 1, "title": "Buoi Kiem Tra Thong Tin SV"},
            teacher,
            expected=201,
        )
        session_id = session["id"]

        # View roster
        data = self.request("GET", f"/api/sessions/{session_id}/attendees", token=teacher)
        self.assertEqual(data["session_id"], session_id)
        attendees = data["attendees"]
        self.assertGreaterEqual(len(attendees), 2)

        # Check student info exists
        sv01 = next(a for a in attendees if a["student_username"] in ("sv01", "243148201076") or a.get("alias") == "sv01")
        self.assertEqual(sv01["name"], "Trần Gia Bảo")
        self.assertEqual(sv01["student_id"], "243148201076")
        self.assertFalse(sv01["is_present"])

        # Manual check-in by lecturer
        res = self.request(
            "POST",
            f"/api/sessions/{session_id}/manual-checkin",
            {"student_username": sv01["student_username"]},
            teacher,
            expected=201,
        )
        self.assertIn("Trần Gia Bảo", res["message"])

        # Verify sv01 is now marked present with timestamp and hash
        updated = self.request("GET", f"/api/sessions/{session_id}/attendees", token=teacher)
        sv01_updated = next(a for a in updated["attendees"] if a["student_username"] in ("sv01", "243148201076") or a.get("alias") == "sv01")
        self.assertTrue(sv01_updated["is_present"])
        self.assertIsNotNone(sv01_updated["occurred_at"])
        self.assertIsNotNone(sv01_updated["event_hash"])

    def test_student_registered_and_login_with_msv(self):
        teacher = self.login("gv01", "GVdemo123!")
        
        # 1. Lecturer creates new student account with MSV
        new_msv = "243148201999"
        res = self.request(
            "POST",
            "/api/students",
            {
                "student_id": new_msv,
                "name": "Đoàn Văn Hậu",
                "class_name": "CNTT2.K25",
                "password": "SVsecret123!"
            },
            teacher,
            expected=201
        )
        self.assertEqual(res["student"]["student_id"], new_msv)
        self.assertTrue(res["student"]["wallet"].startswith("0x"))

        # 2. Student logs in directly using MSV
        student_token = self.login(new_msv, "SVsecret123!")
        me = self.request("GET", "/api/me", token=student_token)
        self.assertEqual(me["name"], "Đoàn Văn Hậu")
        self.assertEqual(me["student_id"], new_msv)
        self.assertEqual(me["role"], "student")


if __name__ == "__main__":
    unittest.main(verbosity=2)
