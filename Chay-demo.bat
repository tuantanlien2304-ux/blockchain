@echo off
setlocal
cd /d "%~dp0"

where py >nul 2>nul
if %errorlevel%==0 (
    set "PYTHON=py -3"
) else (
    set "PYTHON=python"
)

%PYTHON% --version
if errorlevel 1 (
    echo Khong tim thay Python. Cai Python 3 tu https://www.python.org/downloads/ roi chay lai.
    pause
    exit /b 1
)

%PYTHON% -m pip install -r requirements.txt
if errorlevel 1 (
    echo Cai thu vien that bai. Hay kiem tra ket noi Internet va thu chay lai.
    pause
    exit /b 1
)

%PYTHON% app.py
if errorlevel 1 (
    echo Ung dung ket thuc do loi. Kiem tra thong bao o tren.
    pause
    exit /b 1
)
