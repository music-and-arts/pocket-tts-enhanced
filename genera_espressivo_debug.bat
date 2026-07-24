@echo off
setlocal EnableDelayedExpansion
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
set "INPUT_FILE=%~1"
if not "!INPUT_FILE!"=="" goto :got_input
set /p INPUT_FILE="Nome del file di testo da leggere (es. capitolo1.txt): "

:got_input
REM Rimuove eventuali virgolette in eccesso (es. se il percorso e' stato
REM trascinato dentro la finestra della console invece che sull'icona del .bat)
set "INPUT_FILE=!INPUT_FILE:"=!"

if exist "!INPUT_FILE!" goto :input_ok
echo.
echo File "!INPUT_FILE!" non trovato.
echo.
pause
exit /b

:input_ok
for %%F in ("!INPUT_FILE!") do set "DEFAULT_OUTPUT=%%~nF.wav"

set /p OUTPUT_FILE="Nome del file audio di output [invio per: !DEFAULT_OUTPUT!]: "
if "!OUTPUT_FILE!"=="" set "OUTPUT_FILE=!DEFAULT_OUTPUT!"

echo.
echo Genero l'audio da "!INPUT_FILE!" a "!OUTPUT_FILE!" (parametri espliciti sotto, 6 worker)...
echo.

python pocket_tts_expressive.py "!INPUT_FILE!" "!OUTPUT_FILE!" ^
    --language italian_24l --voice giovanni ^
    --base-temperature 0.7 --base-speed 1.0 ^
    --eos-threshold -3.0 --frames-after-eos 8 ^
    --max-words 18 --lsd-decode-steps 1 ^
    --workers 6 --debug

echo.
echo ========================================
echo Fatto! Controlla il file: !OUTPUT_FILE!
echo Manifest e wav singoli nella cartella con suffisso _debug
echo ========================================
pause
