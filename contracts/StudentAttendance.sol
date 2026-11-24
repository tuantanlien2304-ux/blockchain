// SPDX-License-Identifier: MIT
pragma solidity ^0.8.20;

/**
 * @title StudentAttendance
 * @author Nhóm Sinh Viên CNTT - Trường Đại Học Hải Phòng
 * @notice Hệ thống điểm danh sinh viên phi tập trung dựa trên Smart Contract Ethereum.
 * @dev Áp dụng mẫu Checks-Effects-Interactions, kiểm soát phân quyền RBAC và chống tấn công Replay/Reentrancy.
 */
contract StudentAttendance {
    // ==========================================
    // 1. KHAI BÁO CÁC CẤU TRÚC DỮ LIỆU (STRUCTS)
    // ==========================================

    enum SessionStatus { Closed, Open }

    struct ClassInfo {
        string classCode;         // Mã lớp (VD: "CNTT2-K25")
        string courseCode;        // Mã học phần (VD: "BLOCKCHAIN101")
        string courseName;        // Tên học phần
        address lecturer;         // Địa chỉ ví Giảng viên phụ trách
        bool exists;              // Cờ đánh dấu lớp tồn tại
    }

    struct SessionInfo {
        uint256 sessionId;        // ID buổi học
        string classCode;         // Thuộc lớp học nào
        string title;             // Tiêu đề buổi học (VD: "Buổi 1: Tổng quan Ethereum")
        uint256 startTime;        // Thời điểm bắt đầu (Unix timestamp)
        uint256 endTime;          // Thời điểm kết thúc (Unix timestamp)
        bytes32 sessionSecretHash;// Hash của mã bí mật/QR (keccak256(secret))
        SessionStatus status;     // Trạng thái buổi học (Open / Closed)
        uint256 totalPresent;     // Tổng số sinh viên đã điểm danh
    }

    struct AttendanceRecord {
        bool isPresent;           // Đã điểm danh hay chưa
        uint256 timestamp;        // Thời điểm ghi nhận on-chain
        bytes32 verificationHash;// Hash băm chứng thực (Sinh viên + Buổi + Thời gian + Hash trước)
    }

    // ==========================================
    // 2. BIẾN TRẠNG THÁI (STATE VARIABLES)
    // ==========================================

    address public admin; // Quản trị viên hệ thống (Nhà trường/Khoa CNTT)
    uint256 public nextSessionId = 1;
    bytes32 public latestGlobalHash = bytes32(0); // Hash chain on-chain

    // Mapping phân quyền
    mapping(address => bool) public isLecturer;
    mapping(address => string) public studentIds; // Ví sinh viên => Mã SV (VD: "24314820105")
    mapping(string => address) public studentWallets; // Mã SV => Địa chỉ ví

    // Lưu trữ lớp học: classCode => ClassInfo
    mapping(string => ClassInfo) public classes;
    string[] public classList;

    // Ghi danh sinh viên vào lớp: classCode => mapping(address => bool)
    mapping(string => mapping(address => bool)) public isEnrolled;

    // Lưu trữ buổi học: sessionId => SessionInfo
    mapping(uint256 => SessionInfo) public sessions;

    // Lưu trữ bản ghi điểm danh: sessionId => mapping(studentAddress => AttendanceRecord)
    mapping(uint256 => mapping(address => AttendanceRecord)) public attendanceRecords;

    // ==========================================
    // 3. SỰ KIỆN AUDIT TRAIL (EVENTS)
    // ==========================================

    event LecturerAdded(address indexed lecturer);
    event LecturerRemoved(address indexed lecturer);
    event StudentRegistered(address indexed studentWallet, string studentId);
    
    event ClassCreated(string indexed classCode, string courseCode, string courseName, address indexed lecturer);
    event StudentEnrolled(string indexed classCode, address indexed student);

    event SessionCreated(uint256 indexed sessionId, string indexed classCode, string title, uint256 endTime);
    event SessionClosed(uint256 indexed sessionId, uint256 totalPresent);

    event AttendanceCheckedIn(
        uint256 indexed sessionId,
        address indexed student,
        string studentId,
        uint256 timestamp,
        bytes32 verificationHash,
        bytes32 previousHash
    );

    // ==========================================
    // 4. CÁC MODIFIERS KIỂM SOÁT TRUY CẬP (ACCESS CONTROL)
    // ==========================================

    modifier onlyAdmin() {
        require(msg.sender == admin, "Attendance: Chi Admin/Nha truong co quyen");
        _;
    }

    modifier onlyLecturer() {
        require(isLecturer[msg.sender] || msg.sender == admin, "Attendance: Chi Giang vien phu trach co quyen");
        _;
    }

    modifier classExists(string memory _classCode) {
        require(classes[_classCode].exists, "Attendance: Lop hoc khong ton tai");
        _;
    }

    modifier onlyClassLecturer(string memory _classCode) {
        require(
            classes[_classCode].lecturer == msg.sender || msg.sender == admin,
            "Attendance: Ban khong phai giang vien phu trach lop nay"
        );
        _;
    }

    modifier sessionExists(uint256 _sessionId) {
        require(_sessionId > 0 && _sessionId < nextSessionId, "Attendance: Buoi hoc khong ton tai");
        _;
    }

    // ==========================================
    // 5. HÀM KHỞI TẠO (CONSTRUCTOR)
    // ==========================================

    constructor() {
        admin = msg.sender;
        isLecturer[msg.sender] = true; // Admin đồng thời có quyền Giảng viên mặc định
        emit LecturerAdded(msg.sender);
    }

    // ==========================================
    // 6. QUẢN TRỊ DANH TÍNH & PHÂN QUYỀN
    // ==========================================

    function addLecturer(address _lecturer) external onlyAdmin {
        require(_lecturer != address(0), "Dia chi khong hop le");
        require(!isLecturer[_lecturer], "Giang vien da ton tai");
        isLecturer[_lecturer] = true;
        emit LecturerAdded(_lecturer);
    }

    function removeLecturer(address _lecturer) external onlyAdmin {
        require(isLecturer[_lecturer], "Khong phai giang vien");
        require(_lecturer != admin, "Khong the go quyen Admin");
        isLecturer[_lecturer] = false;
        emit LecturerRemoved(_lecturer);
    }

    function registerStudent(address _studentWallet, string calldata _studentId) external onlyAdmin {
        require(_studentWallet != address(0), "Dia chi vi khong hop le");
        require(bytes(_studentId).length > 0, "Ma SV khong duoc de trong");
        
        studentIds[_studentWallet] = _studentId;
        studentWallets[_studentId] = _studentWallet;
        emit StudentRegistered(_studentWallet, _studentId);
    }

    // ==========================================
    // 7. QUẢN LÝ LỚP HỌC & GHI DANH
    // ==========================================

    function createClass(
        string calldata _classCode,
        string calldata _courseCode,
        string calldata _courseName,
        address _lecturer
    ) external onlyLecturer {
        require(!classes[_classCode].exists, "Lop hoc da ton tai tren he thong");
        require(_lecturer != address(0), "Dia chi giang vien khong hop le");
        require(isLecturer[_lecturer], "Dia chi duoc chi dinh chua duoc cap quyen giang vien");

        classes[_classCode] = ClassInfo({
            classCode: _classCode,
            courseCode: _courseCode,
            courseName: _courseName,
            lecturer: _lecturer,
            exists: true
        });

        classList.push(_classCode);
        emit ClassCreated(_classCode, _courseCode, _courseName, _lecturer);
    }

    function enrollStudent(string calldata _classCode, address _student)
        external
        classExists(_classCode)
        onlyClassLecturer(_classCode)
    {
        require(bytes(studentIds[_student]).length > 0, "Sinh vien chua dang ky danh tinh tren he thong");
        require(!isEnrolled[_classCode][_student], "Sinh vien da ghi danh vao lop nay truoc do");

        isEnrolled[_classCode][_student] = true;
        emit StudentEnrolled(_classCode, _student);
    }

    function batchEnrollStudents(string calldata _classCode, address[] calldata _students)
        external
        classExists(_classCode)
        onlyClassLecturer(_classCode)
    {
        for (uint256 i = 0; i < _students.length; i++) {
            address student = _students[i];
            if (bytes(studentIds[student]).length > 0 && !isEnrolled[_classCode][student]) {
                isEnrolled[_classCode][student] = true;
                emit StudentEnrolled(_classCode, student);
            }
        }
    }

    // ==========================================
    // 8. QUẢN LÝ BUỔI HỌC (SESSIONS)
    // ==========================================

    /**
     * @notice Mở một buổi học mới kèm mã hash bí mật (QR hash) và thời gian hiệu lực
     */
    function openSession(
        string calldata _classCode,
        string calldata _title,
        uint256 _durationInSeconds,
        bytes32 _sessionSecretHash
    ) external classExists(_classCode) onlyClassLecturer(_classCode) returns (uint256) {
        require(_durationInSeconds > 0, "Thoi luong phai lon hon 0");

        uint256 sessionId = nextSessionId++;
        uint256 startTime = block.timestamp;
        uint256 endTime = startTime + _durationInSeconds;

        sessions[sessionId] = SessionInfo({
            sessionId: sessionId,
            classCode: _classCode,
            title: _title,
            startTime: startTime,
            endTime: endTime,
            sessionSecretHash: _sessionSecretHash,
            status: SessionStatus.Open,
            totalPresent: 0
        });

        emit SessionCreated(sessionId, _classCode, _title, endTime);
        return sessionId;
    }

    /**
     * @notice Đóng buổi học (chốt sổ điểm danh)
     */
    function closeSession(uint256 _sessionId) external sessionExists(_sessionId) {
        SessionInfo storage session = sessions[_sessionId];
        require(
            classes[session.classCode].lecturer == msg.sender || msg.sender == admin,
            "Chi giang vien phu trach moi duoc phep dong buoi hoc"
        );
        require(session.status == SessionStatus.Open, "Buoi hoc da o trang thai dong");

        session.status = SessionStatus.Closed;
        emit SessionClosed(_sessionId, session.totalPresent);
    }

    // ==========================================
    // 9. THỰC HIỆN ĐIỂM DANH (CHECK-IN)
    // ==========================================

    /**
     * @notice Sinh viên tự điểm danh bằng mã bí mật (từ QR code hoặc mã do giảng viên cung cấp)
     * @dev Áp dụng Checks-Effects-Interactions & Hash-chain liên tục
     */
    function checkIn(uint256 _sessionId, string calldata _secret)
        external
        sessionExists(_sessionId)
    {
        SessionInfo storage session = sessions[_sessionId];

        // 1. CHECKS (Kiểm tra điều kiện)
        require(session.status == SessionStatus.Open, "Buoi hoc da ket thuc hoac chua mo");
        require(block.timestamp <= session.endTime, "Ma diem danh buoi hoc da het han");
        require(
            keccak256(abi.encodePacked(_secret)) == session.sessionSecretHash,
            "Ma xac thuc QR/Bi mat khong chinh xac"
        );
        require(
            isEnrolled[session.classCode][msg.sender],
            "Sinh vien chua duoc ghi danh trong danh sach lop hoc nay"
        );
        require(
            !attendanceRecords[_sessionId][msg.sender].isPresent,
            "Sinh vien da diem danh thanh cong truoc do"
        );

        // 2. EFFECTS (Cập nhật trạng thái trước khi phát tán sự kiện)
        string memory sId = studentIds[msg.sender];
        bytes32 prevHash = latestGlobalHash;
        uint256 currentTime = block.timestamp;

        // Tạo băm mã hóa chứng thực bản ghi (Gắn kết thời gian, mã sinh viên, buổi học và block hash)
        bytes32 verificationHash = keccak256(
            abi.encodePacked(
                _sessionId,
                msg.sender,
                sId,
                currentTime,
                prevHash,
                block.prevrandao
            )
        );

        attendanceRecords[_sessionId][msg.sender] = AttendanceRecord({
            isPresent: true,
            timestamp: currentTime,
            verificationHash: verificationHash
        });

        session.totalPresent += 1;
        latestGlobalHash = verificationHash;

        // 3. INTERACTIONS / LOGS (Phát sự kiện On-chain lưu trữ nhật ký vĩnh viễn)
        emit AttendanceCheckedIn(
            _sessionId,
            msg.sender,
            sId,
            currentTime,
            verificationHash,
            prevHash
        );
    }

    /**
     * @notice Giảng viên có quyền điểm danh thủ công/bổ sung cho sinh viên gặp sự cố thiết bị
     */
    function manualCheckIn(uint256 _sessionId, address _student)
        external
        sessionExists(_sessionId)
    {
        SessionInfo storage session = sessions[_sessionId];
        require(
            classes[session.classCode].lecturer == msg.sender || msg.sender == admin,
            "Chi giang vien lop hoc co the diem danh bo sung"
        );
        require(session.status == SessionStatus.Open, "Buoi hoc da bi dong");
        require(isEnrolled[session.classCode][_student], "Sinh vien chua ghi danh vao lop");
        require(!attendanceRecords[_sessionId][_student].isPresent, "Sinh vien da duoc diem danh");

        string memory sId = studentIds[_student];
        bytes32 prevHash = latestGlobalHash;
        uint256 currentTime = block.timestamp;

        bytes32 verificationHash = keccak256(
            abi.encodePacked(
                _sessionId,
                _student,
                sId,
                currentTime,
                prevHash,
                "MANUAL_LECTURER_OVERRIDE"
            )
        );

        attendanceRecords[_sessionId][_student] = AttendanceRecord({
            isPresent: true,
            timestamp: currentTime,
            verificationHash: verificationHash
        });

        session.totalPresent += 1;
        latestGlobalHash = verificationHash;

        emit AttendanceCheckedIn(
            _sessionId,
            _student,
            sId,
            currentTime,
            verificationHash,
            prevHash
        );
    }

    // ==========================================
    // 10. HÀM TRUY VẤN VÀ BÁO CÁO (GETTERS & VIEWS)
    // ==========================================

    function isStudentPresent(uint256 _sessionId, address _student) external view returns (bool) {
        return attendanceRecords[_sessionId][_student].isPresent;
    }

    function getAttendanceDetail(uint256 _sessionId, address _student)
        external
        view
        returns (bool isPresent, uint256 timestamp, bytes32 verificationHash)
    {
        AttendanceRecord memory record = attendanceRecords[_sessionId][_student];
        return (record.isPresent, record.timestamp, record.verificationHash);
    }

    function getClassCount() external view returns (uint256) {
        return classList.length;
    }
}
