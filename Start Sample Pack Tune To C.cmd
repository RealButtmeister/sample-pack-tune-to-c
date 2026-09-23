@echo off
setlocal
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
  ".venv\Scripts\python.exe" "tools\sample_pack_tune_to_c_gui.py"
  goto done
)
where py >nul 2>nul
if errorlevel 1 goto use_python
py -3 "tools\sample_pack_tune_to_c_gui.py"
goto done
:use_python
python "tools\sample_pack_tune_to_c_gui.py"
:done
if errorlevel 1 pause
