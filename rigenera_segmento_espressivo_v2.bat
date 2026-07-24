@echo off
setlocal EnableDelayedExpansion
title Pocket TTS - Rigenera Segmento Singolo (v3)
cd /d "%~dp0"

echo ========================================
echo    Pocket TTS - Rigenera Segmento Singolo
echo    (usa rigenera_segmento.py e pydub)
echo ========================================
echo.

call .venv\Scripts\activate
uv pip install pydub --quiet

:: Richiedi il file di testo originale (solo per ricavare il nome di default)
set "INPUT_FILE=%~1"
if not "!INPUT_FILE!"=="" goto :check_input
set /p INPUT_FILE="Nome del file di testo originale (es. capitolo1.txt): "

:check_input
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

for %%F in ("!OUTPUT_FILE!") do set "DEFAULT_DEBUG_DIR=%%~nF_debug"
set /p DEBUG_DIR="Cartella _debug del run precedente [invio per: !DEFAULT_DEBUG_DIR!]: "
if "!DEBUG_DIR!"=="" set "DEBUG_DIR=!DEFAULT_DEBUG_DIR!"

if exist "!DEBUG_DIR!" goto :debug_dir_ok
echo.
echo Cartella "!DEBUG_DIR!" non trovata.
pause
exit /b

:debug_dir_ok
if exist "!DEBUG_DIR!\manifest.csv" goto :manifest_ok
echo.
echo File manifest.csv non trovato in "!DEBUG_DIR!".
pause
exit /b

:manifest_ok
echo.
echo Apri "!DEBUG_DIR!\manifest.csv" per vedere l'indice dei segmenti.
set /p SEGMENT="Indice del segmento da rigenerare: "
if not "!SEGMENT!"=="" goto :segment_ok
echo Nessun indice inserito.
pause
exit /b

:segment_ok
:: Parametri regolabili (NON usare TEMP)
set /p TEMPERATURA="Temperatura (default 0.3, piu' bassa = piu' stabile): "
if "!TEMPERATURA!"=="" set "TEMPERATURA=0.3"
set /p VELOCITA="Velocita' (default 0.9, valori <1 rallentano): "
if "!VELOCITA!"=="" set "VELOCITA=0.9"
set /p SEED="Seed specifico (opzionale, INVIO per casuale): "

echo.
echo Rigenero il segmento !SEGMENT! con T=!TEMPERATURA!, S=!VELOCITA! e poi ricompongo...
echo.

python rigenera_segmento.py "!DEBUG_DIR!" "!SEGMENT!" "!OUTPUT_FILE!" "!TEMPERATURA!" "!VELOCITA!" "!SEED!"

echo.
echo ========================================
echo Fatto! Controlla il file: !OUTPUT_FILE!
echo Il segmento !SEGMENT! e' stato rigenerato e il WAV finale ricostruito.
echo Se il risultato non ti soddisfa, riprova con temperatura ancora piu' bassa
echo (es. 0.2) o varia velocita'/seed.
echo ========================================
pause
