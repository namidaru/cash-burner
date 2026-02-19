@echo off
setlocal enabledelayedexpansion

cd /d %~dp0\..

for /f "delims=" %%b in ('git branch --show-current 2^>nul') do set BRANCH=%%b
if "%BRANCH%"=="" (
  echo [ERROR] 현재 git 브랜치를 찾지 못했습니다.
  echo 저장소 루트에서 다시 실행하세요.
  exit /b 1
)

echo [INFO] current branch: %BRANCH%

for /f "tokens=*" %%r in ('git remote') do set HAS_REMOTE=1
if not defined HAS_REMOTE (
  echo [ERROR] git remote가 없습니다.
  echo 예: git remote add origin https://github.com/namidaru/cash-burner.git
  exit /b 1
)

git remote get-url origin >nul 2>nul
if errorlevel 1 (
  echo [WARN] origin remote가 없어 첫번째 remote를 사용합니다.
  for /f "tokens=1" %%n in ('git remote') do (
    set REMOTE_NAME=%%n
    goto :remote_done
  )
) else (
  set REMOTE_NAME=origin
)
:remote_done

echo [INFO] remote: %REMOTE_NAME%
git fetch %REMOTE_NAME% --prune
if errorlevel 1 (
  echo [ERROR] fetch 실패. 네트워크/권한을 확인하세요.
  exit /b 1
)

git push -u %REMOTE_NAME% %BRANCH%
if errorlevel 1 (
  echo [ERROR] push 실패.
  echo - 브랜치명을 잘못 입력했는지 확인하세요. (예: work 와 wk 혼동)
  echo - 원격 저장소 권한/인증을 확인하세요.
  exit /b 1
)

echo [OK] push 완료: %REMOTE_NAME%/%BRANCH%
echo 다음 단계: GitHub에서 Compare ^& pull request를 누르거나,
echo gh CLI가 있으면 아래 명령 사용:
echo gh pr create --base main --head %BRANCH% --fill

endlocal
