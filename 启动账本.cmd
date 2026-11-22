@echo off
setlocal
pushd "%~dp0"
if not exist "runtime\python.exe" goto missing
"runtime\python.exe" -B -X utf8 "ledger_app.py" %*
if errorlevel 1 pause
popd
exit /b
:missing
echo Portable runtime missing. Extract the complete Windows ZIP first.
pause
popd
exit /b 1
