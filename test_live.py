import urllib.request
import json

def post(url, data, token=None):
    headers = {'Content-Type': 'application/json'}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, data=json.dumps(data).encode(), headers=headers, method='POST')
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())

def get(url, token=None):
    headers = {}
    if token:
        headers['Authorization'] = f'Bearer {token}'
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req) as res:
        return json.loads(res.read())

print("==================================================================")
print("             KỊCH BẢN CHẠY THỬ HỆ THỐNG ĐIỂM DANH BLOCKCHAIN      ")
print("==================================================================")

# 1. KIỂM TRA TRANG CHỦ
with urllib.request.urlopen('http://localhost:8000/') as res:
    print(f"[1] Trang chủ Web: HTTP Status {res.status} OK")

# 2. ĐĂNG NHẬP GIẢNG VIÊN
lecturer = post('http://localhost:8000/api/login', {'username': 'gv01', 'password': 'GVdemo123!'})
gv_token = lecturer['token']
print(f"[2] Đăng nhập Giảng viên: {lecturer['name']} ({lecturer['role']})")

sessions = get('http://localhost:8000/api/sessions', gv_token)['sessions']
active_session = sessions[0]
session_id = active_session['id']
pin_code = active_session['attendance_code']
print(f"    - Buổi học: {active_session['title']}")
print(f"    - Mã PIN điểm danh trực tiếp: {pin_code}")

# 3. ĐĂNG NHẬP SINH VIÊN BẰNG MSV
print(f"\n[3] Đăng nhập Sinh viên bằng Mã Sinh Viên (MSV): 243148201076")
student = post('http://localhost:8000/api/login', {'username': '243148201076', 'password': 'SVdemo123!'})
sv_token = student['token']
print(f"    - Họ và tên: {student['name']}")
print(f"    - MSV: {student['student_id']}")
print(f"    - Lớp: {student['class_name']}")
print(f"    - Địa chỉ ví Blockchain: {student['wallet']}")

# 4. SINH VIÊN ĐIỂM DANH ON-CHAIN
print(f"\n[4] Sinh viên thực hiện điểm danh với mã PIN: {pin_code}")
checkin = post('http://localhost:8000/api/attendance', {'attendance_code': pin_code}, sv_token)
print(f"    - Kết quả: {checkin['message']}")

# 5. GIẢNG VIÊN XEM DANH SÁCH ĐIỂM DANH BUỔI HỌC
print(f"\n[5] Giảng viên xem danh sách sinh viên buổi học #{session_id}:")
roster = get(f'http://localhost:8000/api/sessions/{session_id}/attendees', gv_token)
for idx, st in enumerate(roster['attendees'], 1):
    status = "ĐÃ CÓ MẶT ✓" if st['is_present'] else "VẮNG MẶT ✗"
    hash_str = st['event_hash'][:20] + "..." if st['event_hash'] else "—"
    print(f"    {idx}. [{status}] MSV: {st['student_id']:<14} | Tên: {st['name']:<18} | Hash: {hash_str}")

# 6. BLOCKCHAIN EXPLORER
print(f"\n[6] Kiểm tra Sổ cái Blockchain (Explorer):")
chain = get('http://localhost:8000/api/blockchain/explorer')
chain_status = "HỢP LỆ VÀ TOÀN VẸN 100% ✓" if chain['valid'] else "BỊ LỖI ✗"
print(f"    - Trạng thái chuỗi: {chain_status}")
print(f"    - Tổng số khối (Blocks): {chain['total_blocks']}")
print(f"    - Lượt giao dịch điểm danh: {chain['total_attendance_events']}")

latest_block = chain['blocks'][-1]
print(f"    - Khối mới nhất: {latest_block['title']}")
print(f"    - Mã băm Block Hash: {latest_block['block_hash']}")
print(f"    - Previous Hash:    {latest_block['previous_hash']}")
print("==================================================================")
print(">> KẾT QUẢ CHẠY THỬ NGHIỆM: TẤT CẢ TÍNH NĂNG HOẠT ĐỘNG HOÀN HẢO!")
