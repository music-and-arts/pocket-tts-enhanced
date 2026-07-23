@echo off
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
if "%~1"=="" (
    set /p INPUT_FILE="Nome del file di testo originale (es. capitolo1.txt): "
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

for %%F in ("%OUTPUT_FILE%") do set "DEFAULT_DEBUG_DIR=%%~nF_debug"
set /p DEBUG_DIR="Cartella _debug del run precedente [invio per: %DEFAULT_DEBUG_DIR%]: "
if "%DEBUG_DIR%"=="" set "DEBUG_DIR=%DEFAULT_DEBUG_DIR%"

if not exist "%DEBUG_DIR%" (
    echo.
    echo Cartella "%DEBUG_DIR%" non trovata.
    pause
    exit /b
)
if not exist "%DEBUG_DIR%\manifest.csv" (
    echo.
    echo File manifest.csv non trovato in "%DEBUG_DIR%".
    pause
    exit /b
)

echo.
echo Apri "%DEBUG_DIR%\manifest.csv" per vedere l'indice dei segmenti.
set /p SEGMENT="Indice del segmento da rigenerare: "
if "%SEGMENT%"=="" (
    echo Nessun indice inserito.
    pause
    exit /b
)

:: Parametri regolabili (NON usare TEMP)
set /p TEMPERATURA="Temperatura (default 0.3, piu' bassa = piu' stabile): "
if "%TEMPERATURA%"=="" set "TEMPERATURA=0.3"
set /p VELOCITA="Velocita' (default 0.9, valori <1 rallentano): "
if "%VELOCITA%"=="" set "VELOCITA=0.9"
set /p SEED="Seed specifico (opzionale, INVIO per casuale): "

echo.
echo Rigenero il segmento %SEGMENT% con T=%TEMPERATURA%, S=%VELOCITA% e poi ricompongo...
echo.

:: Chiama lo script Python esterno (che ora usa sys.executable)
python rigenera_segmento.py "%DEBUG_DIR%" "%SEGMENT%" "%OUTPUT_FILE%" "%TEMPERATURA%" "%VELOCITA%" "%SEED%"

echo.
echo ========================================
echo Fatto! Controlla il file: %OUTPUT_FILE%
echo Il segmento %SEGMENT% è stato rigenerato e il WAV finale ricostruito.
echo Se il risultato non ti soddisfa, riprova con temperatura ancora più bassa
echo (es. 0.2) o varia velocità/seed.
echo ========================================
pause