@echo off
chcp 65001 >nul
REM claude CLI 로그인 — 봇이 실제로 쓰는 claude.exe 로 로그인한다.
REM PATH 의 claude 와 MSIX 패키지 경로가 갈리면 자격증명이 따로 저장된다.
REM 경로는 src.agents.llm.resolve_claude_command 와 같은 which_claude.py 로 구한다.
setlocal
cd /d "%~dp0\.."

set "PY=%~dp0..\.venv\Scripts\python.exe"
if not exist "%PY%" (
    echo [오류] venv python 을 찾지 못했습니다: %PY%
    pause
    exit /b 1
)

for /f "usebackq delims=" %%i in (`"%PY%" scripts\which_claude.py`) do set "CEXE=%%i"

if not defined CEXE (
    echo [오류] 봇이 쓰는 claude.exe 를 찾지 못했습니다.
    echo        python scripts\which_claude.py 를 직접 실행해 원인을 확인하세요.
    pause
    exit /b 1
)

echo ============================================================
echo  봇이 실제로 사용하는 claude:
echo    %CEXE%
echo.
echo  화면이 뜨면  /login  입력 후 "Claude 구독으로 로그인"(API 키 아님) 선택.
echo  로그인이 끝나면  /exit  로 빠져나오세요.
echo ============================================================
echo.
"%CEXE%"

echo.
echo ------------------------------------------------------------
echo  로그인 확인 중...
"%PY%" scripts\which_claude.py --check
echo ------------------------------------------------------------
pause
endlocal
