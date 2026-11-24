@echo off
chcp 65001 >nul
cd /d "%~dp0"

rem --- Tự tìm IP LAN của máy này ---
for /f "tokens=2 delims=:" %%A in ('ipconfig ^| findstr /c:"IPv4"') do (
    set "IP=%%A"
    goto :gotip
)
:gotip
set "IP=%IP: =%"
if "%IP%"=="" set "IP=127.0.0.1"

set "ATTENDANCE_HOST=0.0.0.0"
set "ATTENDANCE_PORT=8000"
set "ATTENDANCE_BASE_URL=http://%IP%:8000"
set "ATTENDANCE_SECRET_KEY=demo-diem-danh-blockchain-hpu-2026"

echo ==================================================================
echo   DIEM DANH BLOCKCHAIN - CHIA SE TRONG MANG LAN
echo   Link gui cho moi nguoi:  %ATTENDANCE_BASE_URL%
echo   (May tinh/dien thoai phai cung mang Wi-Fi / LAN)
echo ==================================================================
echo   Neu trinh duyet bao khong vao duoc: mo Windows Defender Firewall
echo   -^> Allow an app -^> cho phep python.exe, hoac chay file nay
echo   bang quyen Administrator (Run as administrator).
echo ==================================================================

python -c "import qrcode, PIL, zxingcpp" 2>nul || python -m pip install -r requirements.txt
python app.py
pause
