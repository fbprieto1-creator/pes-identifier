@echo off
set OPENAI_LOCAL_BASE_URL=http://localhost:11434
set OPENAI_LOCAL_MODEL=qwen3-vl:2b-instruct
python "%~dp0main.py"
if errorlevel 1 pause
