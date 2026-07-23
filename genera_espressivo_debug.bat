@echo off
title Pocket TTS - Modalita' Espressiva (con debug)
cd /d "%~dp0"

echo ========================================
echo    Pocket TTS - Espressiva + DEBUG
echo    (manifest.csv + wav singoli per segmento)
echo ========================================
echo.

call .venv\Scripts\activate

REM Verifica/installa pydub in automatico (istantaneo se gia' presente)
uv pip install pydub --quiet

REM %1 e' il file trascinato sull'icona del .bat (drag & drop).
if "%~1"=="" (
    set /p INPUT_FILE="Nome del file di testo da leggere (es. capitolo1.txt): "
) else (
    set "INPUT_FILE=%~1"
)

if not exist "%INPUT_FILE%" (
    echo.
    echo File "%INPUT_FILE%" non trovato.
    echo.
    pause
    exit /b
)

for %%F in ("%INPUT_FILE%") do set "DEFAULT_OUTPUT=%%~nF.wav"

set /p OUTPUT_FILE="Nome del file audio di output [invio per: %DEFAULT_OUTPUT%]: "
if "%OUTPUT_FILE%"=="" set "OUTPUT_FILE=%DEFAULT_OUTPUT%"

echo.
echo Genero l'audio da "%INPUT_FILE%" a "%OUTPUT_FILE%" (parametri espliciti sotto, 6 worker)...
echo.

REM Parametri esplicitati (invece di lasciarli ai default dello script) cosi'
REM e' sempre chiaro cosa sta usando questo bat, senza dover controllare
REM i default correnti in pocket_tts_expressive.py. --debug sempre attivo:
REM genera manifest.csv + wav singoli in "<output>_debug\".
python pocket_tts_expressive.py "%INPUT_FILE%" "%OUTPUT_FILE%" ^
    --language italian_24l --voice giovanni ^
    --base-temperature 0.7 --base-speed 1.0 ^
    --eos-threshold -3.0 --frames-after-eos 8 ^
    --max-words 18 --lsd-decode-steps 1 ^
    --workers 6 --debug

echo.
echo ========================================
echo Fatto! Controlla il file: %OUTPUT_FILE%
echo Manifest e wav singoli in: %OUTPUT_FILE:~0,-4%_debug\
echo ========================================
pause
