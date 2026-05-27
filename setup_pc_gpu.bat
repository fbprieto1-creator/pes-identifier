@echo off
setlocal

echo Instalando dependencias Python...
py -m pip install --upgrade pip
py -m pip install -r "%~dp0requirements.txt"
if errorlevel 1 goto error

echo.
echo Baixando modelo local recomendado para GPU de 3GB...
ollama pull qwen3-vl:2b-instruct
if errorlevel 1 goto error

echo.
echo Pronto. Para abrir o programa, execute run.bat
pause
exit /b 0

:error
echo.
echo Falhou. Verifique se Python e Ollama estao instalados e no PATH.
pause
exit /b 1
