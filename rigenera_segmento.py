# rigenera_segmento.py - DEFINITIVO
import os
import sys
import csv
import subprocess
import tempfile
import re
from pydub import AudioSegment

def main():
    if len(sys.argv) < 6:
        print("Uso: python rigenera_segmento.py debug_dir indice output_wav temperatura velocita [seed]")
        sys.exit(1)

    debug_dir = sys.argv[1]
    index = int(sys.argv[2])
    output_wav = sys.argv[3]
    temperature = float(sys.argv[4])
    speed = float(sys.argv[5])
    seed = sys.argv[6] if len(sys.argv) > 6 and sys.argv[6] != '' else None

    manifest_path = os.path.join(debug_dir, 'manifest.csv')
    if not os.path.exists(manifest_path):
        print(f"Manifest non trovato: {manifest_path}")
        sys.exit(1)

    # Leggi il manifest
    with open(manifest_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        
        idx_key = None
        file_key = None
        text_key = None
        tipo_key = None
        durata_key = None
        
        for key in reader.fieldnames:
            k = key.lower()
            if 'indice' in k or 'index' in k:
                idx_key = key
            elif 'file' in k:
                file_key = key
            elif 'testo' in k or 'text' in k:
                text_key = key
            elif 'tipo' in k:
                tipo_key = key
            elif 'durata' in k and 'sec' in k:
                durata_key = key

        if idx_key is None or file_key is None or text_key is None or tipo_key is None:
            print("Colonne obbligatorie mancanti nel manifest.")
            print("Colonne presenti:", reader.fieldnames)
            sys.exit(1)

        target_segment = None
        target_text = None
        target_file = None
        target_tipo = None
        
        for row in reader:
            if int(row[idx_key]) == index:
                target_segment = row
                target_text = row[text_key]
                target_file = row[file_key]
                target_tipo = row[tipo_key].strip().lower()
                break

        if target_segment is None:
            print(f"Indice {index} non trovato nel manifest")
            sys.exit(1)

        if target_tipo == 'pausa':
            print(f"L'indice {index} è una pausa, non si rigenera.")
            sys.exit(0)

        segment_wav = os.path.join(debug_dir, target_file)
        print(f"Segmento {index}: {target_file}")
        print(f"Testo: {target_text}")

    # Crea file temporaneo
    with tempfile.NamedTemporaryFile(mode='w', suffix='.txt', delete=False, encoding='utf-8') as temp_txt:
        temp_txt.write(target_text)
        temp_txt_path = temp_txt.name

    # Comando per rigenerare (sovrascrive il file originale)
    cmd = [
        sys.executable, 'pocket_tts_expressive.py',
        temp_txt_path,
        segment_wav,
        '--language', 'italian_24l',
        '--voice', 'giovanni',
        '--base-temperature', str(temperature),
        '--base-speed', str(speed),
        '--eos-threshold', '-3.0',
        '--frames-after-eos', '8',
        '--max-words', '18',
        '--lsd-decode-steps', '1',
        '--workers', '6'
    ]
    if seed is not None:
        cmd.extend(['--seed', seed])

    print(f"Rigenero con T={temperature}, S={speed}, seed={seed}")
    subprocess.run(cmd, check=True)

    # Ricompone il WAV completo
    combined = AudioSegment.empty()
    with open(manifest_path, 'r', encoding='utf-8') as f:
        reader = csv.DictReader(f)
        segments = []
        for row in reader:
            seg_idx = int(row[idx_key])
            seg_tipo = row[tipo_key].strip().lower()
            seg_file = row[file_key]
            durata = float(row[durata_key]) if durata_key and row[durata_key] else 0.0
            segments.append({
                'indice': seg_idx,
                'tipo': seg_tipo,
                'file': seg_file,
                'durata_sec': durata
            })

    segments.sort(key=lambda x: x['indice'])

    for seg in segments:
        if seg['tipo'] == 'pausa':
            silence_duration_ms = int(seg['durata_sec'] * 1000)
            combined += AudioSegment.silent(duration=silence_duration_ms)
            continue

        wav_file = os.path.join(debug_dir, seg['file'])
        if not os.path.exists(wav_file):
            # Fallback: cerca un file alternativo
            found = None
            for f in os.listdir(debug_dir):
                if f.endswith('.wav') and re.search(rf'\b{seg["indice"]}\b', f):
                    found = os.path.join(debug_dir, f)
                    break
            if found:
                wav_file = found
                print(f"Usato fallback: {os.path.basename(wav_file)} per indice {seg['indice']}")
            else:
                raise FileNotFoundError(f"File per indice {seg['indice']} non trovato")

        combined += AudioSegment.from_wav(wav_file)

    combined.export(output_wav, format='wav')
    os.unlink(temp_txt_path)

    print(f"\n✅ Segmento {index} sovrascritto in: {segment_wav}")
    print(f"✅ WAV completo ricostruito: {output_wav}")

if __name__ == "__main__":
    main()