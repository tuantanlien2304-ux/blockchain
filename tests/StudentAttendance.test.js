const { expect } = require("chai");
const { ethers } = require("hardhat");

describe("StudentAttendance Smart Contract Test Suite", function () {
    let StudentAttendance;
    let attendance;
    let admin, lecturer, student1, student2, nonStudent;
    const classCode = "CNTT2-K25";
    const courseCode = "BLOCKCHAIN101";
    const courseName = "Cong Nghe Blockchain";
    const secretCode = "ATTEND_SECRET_987654";
    let secretHash;

    beforeEach(async function () {
        [admin, lecturer, student1, student2, nonStudent] = await ethers.getSigners();
        secretHash = ethers.keccak256(ethers.toUtf8Bytes(secretCode));

        const Factory = await ethers.getContractFactory("StudentAttendance");
        attendance = await Factory.deploy();
        await attendance.waitForDeployment();

        // 1. Cấp quyền Giảng viên
        await attendance.connect(admin).addLecturer(lecturer.address);

        // 2. Đăng ký sinh viên
        await attendance.connect(admin).registerStudent(student1.address, "24314820105");
        await attendance.connect(admin).registerStudent(student2.address, "243148201086");

        // 3. Giảng viên tạo lớp học
        await attendance.connect(lecturer).createClass(classCode, courseCode, courseName, lecturer.address);

        // 4. Ghi danh sinh viên vào lớp
        await attendance.connect(lecturer).enrollStudent(classCode, student1.address);
        await attendance.connect(lecturer).enrollStudent(classCode, student2.address);
    });

    it("TC-01: Khởi tạo hợp đồng và phân quyền Admin/Giảng viên thành công", async function () {
        expect(await attendance.admin()).to.equal(admin.address);
        expect(await attendance.isLecturer(lecturer.address)).to.be.true;
        expect(await attendance.isLecturer(nonStudent.address)).to.be.false;
    });

    it("TC-02: Giảng viên mở buổi học và phát hành mã điểm danh hợp lệ", async function () {
        const duration = 3600; // 1 giờ
        const tx = await attendance.connect(lecturer).openSession(classCode, "Buoi 1: Smart Contracts", duration, secretHash);
        const receipt = await tx.wait();

        const session = await attendance.sessions(1);
        expect(session.sessionId).to.equal(1);
        expect(session.status).to.equal(1); // SessionStatus.Open
        expect(session.totalPresent).to.equal(0);
    });

    it("TC-03: Sinh viên điểm danh thành công với mã bí mật chính xác", async function () {
        await attendance.connect(lecturer).openSession(classCode, "Buoi 1", 3600, secretHash);

        const tx = await attendance.connect(student1).checkIn(1, secretCode);
        await expect(tx)
            .to.emit(attendance, "AttendanceCheckedIn")
            .withArgs(1, student1.address, "24314820105", await ethers.provider.getBlock("latest").then(b => b.timestamp), await attendance.latestGlobalHash(), ethers.ZeroHash);

        expect(await attendance.isStudentPresent(1, student1.address)).to.be.true;
        const session = await attendance.sessions(1);
        expect(session.totalPresent).to.equal(1);
    });

    it("TC-04: Từ chối điểm danh nếu sinh viên nhập sai mã bí mật", async function () {
        await attendance.connect(lecturer).openSession(classCode, "Buoi 1", 3600, secretHash);
        await expect(
            attendance.connect(student1).checkIn(1, "WRONG_SECRET")
        ).to.be.revertedWith("Ma xac thuc QR/Bi mat khong chinh xac");
    });

    it("TC-05: Ngăn chặn điểm danh lặp lại 2 lần (Double Check-in Prevention)", async function () {
        await attendance.connect(lecturer).openSession(classCode, "Buoi 1", 3600, secretHash);
        await attendance.connect(student1).checkIn(1, secretCode);

        await expect(
            attendance.connect(student1).checkIn(1, secretCode)
        ).to.be.revertedWith("Sinh vien da diem danh thanh cong truoc do");
    });

    it("TC-06: Chặn người lạ chưa ghi danh vào lớp tham gia điểm danh", async function () {
        await attendance.connect(lecturer).openSession(classCode, "Buoi 1", 3600, secretHash);
        await expect(
            attendance.connect(nonStudent).checkIn(1, secretCode)
        ).to.be.revertedWith("Sinh vien chua duoc ghi danh trong danh sach lop hoc nay");
    });

    it("TC-07: Chặn điểm danh khi buổi học đã bị Giảng viên đóng", async function () {
        await attendance.connect(lecturer).openSession(classCode, "Buoi 1", 3600, secretHash);
        await attendance.connect(lecturer).closeSession(1);

        await expect(
            attendance.connect(student1).checkIn(1, secretCode)
        ).to.be.revertedWith("Buoi hoc da ket thuc hoac chua mo");
    });

    it("TC-08: Giảng viên có thể điểm danh bù (manualCheckIn) cho sinh viên gặp sự cố", async function () {
        await attendance.connect(lecturer).openSession(classCode, "Buoi 1", 3600, secretHash);
        await attendance.connect(lecturer).manualCheckIn(1, student2.address);

        expect(await attendance.isStudentPresent(1, student2.address)).to.be.true;
        const session = await attendance.sessions(1);
        expect(session.totalPresent).to.equal(1);
    });
});
