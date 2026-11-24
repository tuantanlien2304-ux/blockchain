const { ethers } = require("hardhat");

async function main() {
    console.log("--------------------------------------------------");
    console.log("BAT DAU TRIEN KHAI SMART CONTRACT STUDENT ATTENDANCE");
    console.log("--------------------------------------------------");

    const [deployer] = await ethers.getSigners();
    console.log("Trien khai boi dia chi vi:", deployer.address);

    const balance = await ethers.provider.getBalance(deployer.address);
    console.log("So du tai khoan:", ethers.formatEther(balance), "ETH");

    const StudentAttendance = await ethers.getContractFactory("StudentAttendance");
    const attendance = await StudentAttendance.deploy();
    await attendance.waitForDeployment();

    const address = await attendance.getAddress();
    console.log(">> Smart Contract da duoc trien khai thanh cong tai dia chi:", address);
    console.log(">> Admin mac dinh:", await attendance.admin());
    console.log("--------------------------------------------------");
}

main()
    .then(() => process.exit(0))
    .catch((error) => {
        console.error(error);
        process.exit(1);
    });
