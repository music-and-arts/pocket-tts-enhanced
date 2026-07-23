"""
Pocket TTS - Generatore espressivo con markup leggero e generazione parallela
================================================================================
(vedi versioni precedenti per la sintassi del markup: $temp, £speed, %pausa)

FIX IMPORTANTE (v3): i worker ora caricano il modello UNA SOLA VOLTA
all'avvio del processo (tramite initializer), raggruppati per valore di
temperatura. Questo evita caricamenti ridondanti/scoordinati che su
Windows possono saturare la memoria e causare crash (access violation).
"""

import argparse
import difflib
import re
import sys
from pathlib import Path
from contextlib import ExitStack
from concurrent.futures import ProcessPoolExecutor, as_completed
from collections import defaultdict

import numpy as np


# ----------------------------------------------------------------------
# Parsing del markup leggero (invariato)
# ----------------------------------------------------------------------

TOKEN_PATTERN = re.compile(
    r"\$(?P<temp>\d+\.\d+)"
    r"|£(?P<speed>\d+\.\d+)"
    r"|%(?P<pausa>\d+\.\d+)"
)

# Spezza dopo un punto/esclamativo/interrogativo/ellissi seguito da spazio e
# lettera maiuscola o virgolette - euristica per evitare di spezzare male le
# abbreviazioni, imperfetta ma efficace sulla prosa normale.
SENTENCE_SPLIT = re.compile(r'(?<=[.!?…])\s+(?=[A-ZÀÈÉÌÒÙ"“«])')

# Punti dove e' "lecito" spezzare una frase troppo lunga anche a meta',
# se non c'e' altro modo per stare sotto la soglia di parole.
CLAUSE_SPLIT = re.compile(r'(?<=[,;:])\s+')

# Congiunzioni/preposizioni "leggere" usate come candidati di split
# secondario quando un pezzo supera max_words senza punteggiatura interna
# utile. Spezzare PRIMA di una di queste parole, vicino al centro del
# pezzo, da' quasi sempre un confine grammaticalmente piu' plausibile del
# taglio cieco a meta' conteggio parole - che rischia di interrompere un
# sintagma come "una piaga tipica" esattamente a meta' (caso reale
# osservato nei test: "...è una." / "piaga tipica..." invece di un taglio
# prima di "con" o "per").
SOFT_SPLIT_WORDS = {
    "e", "ma", "però", "che", "con", "mentre", "quando", "dove",
    "come", "perché", "poiché", "se", "anche", "pur", "nonostante",
    "ovvero", "cioè", "quindi", "dunque", "infatti", "affinché",
}

# I puntini di sospensione ("...") vengono letti dal modello con una pausa
# di durata IMPREVEDIBILE (osservato nei test: stessa frase, run diversi,
# pausa da <1s a diversi secondi). Li si convertono in una pausa esplicita
# via markup %secondi, che viene gia' applicata in post con ffmpeg a durata
# esatta - stessa idea, ma deterministica invece che lasciata al modello.
ELLIPSIS_PATTERN = re.compile(r"\.\.\.")


def _convert_ellipsis_to_pause(text, pause_sec, min_words_before=3):
    """Sostituisce ogni '...' con una pausa esplicita di durata fissa
    (markup %pause_sec). Se pause_sec <= 0, la conversione e' disattivata
    e i puntini restano letterali (comportamento precedente).

    ECCEZIONE (scoperta nei test): se il testo IMMEDIATAMENTE prima dei
    puntini ha meno di min_words_before parole (es. "Giacomino,
    Giacomino..."), la conversione NON viene applicata - spezzare li'
    crea un frammento troppo corto (a volte con una parola ripetuta),
    che nei test ha causato un audio peggiore (troncato/roco/volume alto)
    del problema originale che si voleva risolvere. In quel caso i
    puntini restano letterali, come nel comportamento precedente."""
    if pause_sec <= 0:
        return text

    def _replace(match):
        before = text[:match.start()]
        tail = re.split(r"[.!?\n]", before)[-1]
        if len(tail.split()) < min_words_before:
            return match.group(0)
        return f" %{pause_sec} "

    return ELLIPSIS_PATTERN.sub(_replace, text)


def _find_soft_split_point(words):
    """Cerca, tra le parole di un pezzo troppo lungo, il punto piu' vicino
    al centro in cui spezzare PRIMA di una congiunzione/preposizione
    'leggera' (vedi SOFT_SPLIT_WORDS). Ritorna l'indice di split (0 <
    indice < len(words)), o None se non ne trova nessuna entro una finestra
    ragionevole attorno al centro - in quel caso si ricade sul taglio a
    meta' conteggio, invariato rispetto a prima."""
    n = len(words)
    if n < 4:
        return None
    mid = n // 2
    window = max(2, n // 4)  # margine di ricerca attorno al centro
    best_idx = None
    best_dist = None
    for i in range(1, n):
        word_clean = words[i].lower().strip(",.;:!?…\"'")
        if word_clean in SOFT_SPLIT_WORDS:
            dist = abs(i - mid)
            if dist <= window and (best_dist is None or dist < best_dist):
                best_idx = i
                best_dist = dist
    return best_idx


def normalize_text(text):
    """
    Normalizza caratteri tipografici che nei test hanno causato problemi
    concreti (parole saltate, pronuncia errata) - vedi manifest di debug:
    "E'" / "E'" (apostrofo curvo) al posto di "E" accentata sparisce del
    tutto nell'audio generato in piu' di un caso.
    """
    replacements = {
        "E’": "È", "E'": "È",
        "e’": "e'",  # minuscolo lasciato come apostrofo dritto (es. c'e', dell'esistenza)
        "’": "'",
        "‘": "'",
        "“": '"',
        "”": '"',
        "…": "...",
    }
    for old, new in replacements.items():
        text = text.replace(old, new)
    return text


def _ensure_terminal_punctuation(fragment):
    """
    I test hanno mostrato che i frammenti che finiscono a meta' frase
    (con virgola, due punti, punto e virgola) causano quasi sempre rumore
    o allucinazioni in coda - il modello non ha un segnale chiaro di
    'fine parlato'. Sostituendo la punteggiatura di sospensione con un
    punto, il modello chiude l'enunciato in modo netto. Costo accettato:
    una leggera perdita di intonazione "in sospeso" tra una clausola e
    l'altra, a favore dell'affidabilita'.
    """
    fragment = fragment.rstrip()
    if not fragment:
        return fragment
    if fragment[-1] in ",;:":
        fragment = fragment[:-1].rstrip() + "."
    elif fragment[-1] not in ".!?…":
        fragment = fragment + "."
    return fragment


def split_long_sentence(sentence, max_words=18):
    """
    Se una frase (gia' isolata da split_into_sentences) supera max_words,
    la spezza ulteriormente sui punti di clausola (virgola, due punti,
    punto e virgola), raggruppando i pezzi fino ad avvicinarsi al limite,
    e normalizzando la punteggiatura di chiusura di ogni frammento (vedi
    _ensure_terminal_punctuation).
    Necessario perche' in italiano una singola frase grammaticale puo'
    essere lunghissima (subordinate, elenchi) pur essendo "una frase sola"
    - e i test hanno mostrato che oltre le ~20-25 parole il modello
    comincia ad allucinare, saltare parole o alzare il volume in modo
    incontrollato.
    """
    words = sentence.split()
    if len(words) <= max_words:
        return [sentence]

    pieces = CLAUSE_SPLIT.split(sentence)
    if len(pieces) == 1:
        # Nessun punto di clausola su cui spezzare: si tiene cosi' com'e',
        # non c'e' un modo pulito per accorciarla ulteriormente.
        return [sentence]

    result = []
    current = []
    current_words = 0
    for piece in pieces:
        piece_words = len(piece.split())
        if current and current_words + piece_words > max_words:
            result.append(" ".join(current))
            current = [piece]
            current_words = piece_words
        else:
            current.append(piece)
            current_words += piece_words
    if current:
        result.append(" ".join(current))

    # Fallback: se un pezzo "atomico" (senza virgole/due punti interni)
    # supera comunque max_words, si cerca prima un punto di split "leggero"
    # (congiunzione/preposizione vicino al centro, vedi _find_soft_split_point)
    # - da' un confine grammaticale piu' plausibile. Solo se non se ne trova
    # uno entro una finestra ragionevole, si ricade sul taglio a meta' sul
    # conteggio di parole (comportamento originale).
    final_result = []
    for piece in result:
        piece_words = piece.split()
        if len(piece_words) <= max_words:
            final_result.append(piece)
        else:
            split_idx = _find_soft_split_point(piece_words)
            if split_idx is None:
                split_idx = len(piece_words) // 2
            final_result.append(" ".join(piece_words[:split_idx]))
            final_result.append(" ".join(piece_words[split_idx:]))

    return [_ensure_terminal_punctuation(p) for p in final_result]


def split_into_sentences(text):
    text = text.strip()
    if not text:
        return []
    parts = SENTENCE_SPLIT.split(text)
    return [p.strip() for p in parts if p.strip()]


def _append_text_chunk(segments, chunk, temp, speed, max_words=18):
    """Spezza un blocco di testo in frasi (e ulteriormente in sotto-frasi
    se troppo lunghe), tutte con lo stesso temp/speed dato che non ci sono
    tag al loro interno. La punteggiatura finale viene normalizzata SEMPRE
    (non solo sui pezzi che passano dal ramo di split lungo), perche' la
    conversione ellissi->pausa (vedi _convert_ellipsis_to_pause) puo'
    generare frammenti corti senza punto finale."""
    for sentence in split_into_sentences(chunk):
        for sub in split_long_sentence(sentence, max_words):
            segments.append({
                "type": "text", "content": _ensure_terminal_punctuation(sub),
                "temp": temp, "speed": speed,
            })


def parse_markup(text, base_temperature=0.7, base_speed=1.0, max_words=18, ellipsis_pause_sec=0.45):
    text = normalize_text(text)
    text = _convert_ellipsis_to_pause(text, ellipsis_pause_sec)
    segments = []
    # Reset di stato ad ogni "blocco": sia righe vuote (paragrafi classici)
    # sia singole righe (per file dove ogni riga e' un paragrafo, come nel
    # caso tipico di un capitolo scritto una frase/paragrafo per riga).
    paragraphs = re.split(r"\n+", text)

    for paragraph in paragraphs:
        cur_temp = base_temperature
        cur_speed = base_speed
        pos = 0

        for match in TOKEN_PATTERN.finditer(paragraph):
            chunk = paragraph[pos:match.start()].strip()
            if chunk:
                _append_text_chunk(segments, chunk, cur_temp, cur_speed, max_words)

            if match.group("pausa") is not None:
                segments.append({"type": "pause", "duration": float(match.group("pausa"))})
            elif match.group("temp") is not None:
                cur_temp = round(float(match.group("temp")), 1)
            elif match.group("speed") is not None:
                cur_speed = float(match.group("speed"))

            pos = match.end()

        tail = paragraph[pos:].strip()
        if tail:
            _append_text_chunk(segments, tail, cur_temp, cur_speed, max_words)

    return segments


def _assign_lead_ins(segments, mode, static_text, fallback_text, n_words):
    """Calcola e assegna a ogni segmento di tipo 'text' il campo 'lead_in'
    che verra' anteposto (e poi tagliato) prima della generazione.

    PROBLEMA che questo risolve: ogni segmento di testo e' generato come
    un'utterance NUOVA, senza alcun contesto audio/linguistico precedente
    (il modello "parte da zero" a ogni chiamata). E' proprio in questo
    "avvio a freddo" che si concentrano le allucinazioni piu' fastidiose
    osservate nei test: la sillaba o la parola iniziale pronunciata cosi'
    in fretta da risultare quasi inavvertibile. Dato che ogni frase (dopo
    un '.') E ogni sotto-clausola troppo lunga (dopo una ',') diventano
    segmenti separati, il problema si manifesta prevalentemente li' - il che
    corrisponde esattamente a quanto osservato.

    modalita':
    - "context" (default): il lead-in di un segmento sono le ultime
      n_words parole del segmento di testo IMMEDIATAMENTE precedente
      (che nel testo originale lo precedeva davvero). Rispetto a un filler
      fisso e arbitrario (es. "Allora,"), questo da' al modello un contesto
      linguistico VERO su cui "prendere lo slancio" - stesso principio del
      filler, ma con parole che nel testo originale erano gia' seguite da
      quella frase, quindi piu' naturali da continuare. Aiuta anche il
      rilevamento automatico del punto di taglio (vedi _auto_trim_lead_in):
      il confine tra il lead-in e il segmento vero coincide con un vero
      confine di frase/clausola, quindi la pausa naturale del modello li' e'
      piu' marcata e piu' facile da individuare rispetto al confine
      artificiale filler->testo.
    - "static": comportamento precedente, stesso filler fisso per tutti i
      segmenti (--lead-in-text).
    - "off": nessun lead-in (comportamento originale, prima di qualsiasi
      esperimento).

    Per il primissimo segmento di testo del file (nessun precedente
    disponibile) si ricade su fallback_text in ogni modalita' diversa da
    "off"."""
    if mode == "off":
        for seg in segments:
            if seg["type"] == "text":
                seg["lead_in"] = ""
        return

    if mode == "static":
        for seg in segments:
            if seg["type"] == "text":
                seg["lead_in"] = static_text
        return

    # mode == "context"
    prev_text = None
    for seg in segments:
        if seg["type"] != "text":
            continue
        if prev_text is None:
            seg["lead_in"] = fallback_text
        else:
            words = prev_text.split()
            seg["lead_in"] = " ".join(words[-n_words:]) if words else fallback_text
        prev_text = seg["content"]


def _parse_idx_list(spec):
    """Parsa una lista di indici separati da virgola, con range 'a-b' supportati
    (es. '6,8-10' -> {6, 8, 9, 10}). Ritorna None se spec e' vuota (= 'tutti')."""
    if not spec:
        return None
    result = set()
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            result.update(range(int(a), int(b) + 1))
        else:
            result.add(int(part))
    return result


def _parse_seed_overrides(spec):
    """Parsa 'indice:seed,indice:seed' in {indice: seed, ...}."""
    overrides = {}
    if not spec:
        return overrides
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        idx_str, seed_str = part.split(":", 1)
        overrides[int(idx_str)] = int(seed_str)
    return overrides


def _load_reused_segment(idx, reuse_dir):
    """Carica l'audio di un segmento gia' generato in un run precedente con
    --debug, per riusarlo senza rigenerarlo (vedi --only-segments). Cerca il
    file per prefisso di indice: il nome contiene anche temp/speed che qui non
    servono conoscere in anticipo (es. '006_T1.3_S1.0.wav')."""
    from pydub import AudioSegment as _AS
    matches = sorted(Path(reuse_dir).glob(f"{idx:03d}_T*_S*.wav"))
    if not matches:
        print(f"Errore: nessun file trovato in '{reuse_dir}' per il segmento {idx} "
              f"(pattern atteso: {idx:03d}_T*_S*.wav). Verifica che sia la cartella "
              f"'<output>_debug' generata con --debug dal run originale, e che quel "
              f"run avesse lo stesso numero di segmenti di questo (stesso testo di "
              f"partenza).", file=sys.stderr)
        sys.exit(1)
    seg_audio = _AS.from_wav(matches[0])
    sr = seg_audio.frame_rate
    samples = np.array(seg_audio.get_array_of_samples()).astype(np.float32) / 32767.0
    return samples, sr


# ----------------------------------------------------------------------
# Worker: caricamento UNA SOLA VOLTA all'avvio del processo
# ----------------------------------------------------------------------

_MODEL = None
_VOICE_STATE = None


def _worker_init(language, temp, quantize, eos_threshold, voice, lsd_decode_steps):
    """Eseguito una sola volta quando il processo worker viene creato."""
    global _MODEL, _VOICE_STATE
    from pocket_tts import TTSModel
    print(f"[worker pid={__import__('os').getpid()}] Carico modello temp={temp} "
          f"lsd_decode_steps={lsd_decode_steps} ...", file=sys.stderr)
    _MODEL = TTSModel.load_model(
        language=language, temp=temp, quantize=quantize, eos_threshold=eos_threshold,
        lsd_decode_steps=lsd_decode_steps,
    )
    _VOICE_STATE = _MODEL.get_state_for_audio_prompt(voice)


def apply_speed(audio_np, sample_rate, speed):
    if abs(speed - 1.0) < 1e-3:
        return audio_np, sample_rate

    from pydub import AudioSegment
    from pydub.effects import speedup

    int16_audio = (audio_np * 32767).astype(np.int16)
    seg = AudioSegment(int16_audio.tobytes(), frame_rate=sample_rate, sample_width=2, channels=1)

    speed = max(0.5, min(2.0, speed))
    if speed > 1.0:
        try:
            seg = speedup(seg, playback_speed=speed)
        except Exception:
            # pydub.effects.speedup lavora a "chunk" (150ms di default) e
            # lancia un'eccezione se l'audio e' piu' corto di un chunk.
            # Un segmento cosi' corto e' quasi certamente un fallimento del
            # modello a monte (rumore/silenzio quasi totale), non un
            # problema di velocita' - lasciamolo passare invariato: il
            # controllo su durata/parole lo flaggera' e finira' comunque
            # in retry/salvataggio, invece di far crashare tutto il pool
            # di generazione per un singolo segmento.
            pass
    else:
        seg = seg.set_frame_rate(int(seg.frame_rate * speed))
        seg = seg.set_frame_rate(sample_rate)

    samples = np.array(seg.get_array_of_samples()).astype(np.float32) / 32767.0
    return samples, sample_rate


def _auto_trim_lead_in(audio_np, sr, fallback_trim_sec, search_window_sec=2.0,
                        min_silence_ms=150, silence_thresh_offset=16):
    """Cerca automaticamente la fine del filler iniziale (--lead-in-text)
    rilevando la pausa/gap di energia bassa nei primi search_window_sec
    secondi dell'audio generato - tipicamente la micro-pausa naturale
    dopo una virgola nel filler (es. "Allora,"). Taglia l'audio fino alla
    fine di quel gap, invece di un numero di secondi fisso indovinato a
    priori.

    Tra tutti i gap trovati si scegie il PIU' LUNGO (non il primo): i test
    hanno mostrato che il primo gap puo' essere una micro-chiusura interna
    alla parola stessa (es. tra sillabe di "Al-lora"), troppo breve per
    essere la vera pausa dopo il filler - tagliare li' lascia un residuo
    udibile del filler prima del testo vero. Soglia di silenzio innalzata
    (min_silence_ms 150, thresh_offset 16dB) per lo stesso motivo: essere
    piu' selettivi ed escludere le micro-chiusure.

    Se non trova nessun gap che soddisfi la soglia, ricade sul trim fisso
    (fallback_trim_sec) - stessa rete di sicurezza di prima."""
    from pydub import AudioSegment
    from pydub.silence import detect_silence

    int16_audio = (audio_np * 32767).astype(np.int16)
    seg = AudioSegment(int16_audio.tobytes(), frame_rate=sr, sample_width=2, channels=1)

    search_ms = int(search_window_sec * 1000)
    window = seg[:search_ms] if len(seg) > search_ms else seg

    silence_thresh = window.dBFS - silence_thresh_offset if window.dBFS != float("-inf") else -40
    gaps = detect_silence(window, min_silence_len=min_silence_ms, silence_thresh=silence_thresh)

    if gaps:
        # Il gap PIU' LUNGO tra quelli trovati, non il primo - vedi docstring.
        longest_gap = max(gaps, key=lambda g: g[1] - g[0])
        trim_ms = longest_gap[1]
        trim_samples = int(trim_ms / 1000 * sr)
    else:
        trim_samples = int(fallback_trim_sec * sr)

    if trim_samples < len(audio_np):
        return audio_np[trim_samples:]
    return audio_np


def _resample_for_asr(audio_np, sr, target_sr=16000):
    """Resample semplice (interpolazione lineare, solo per la verifica ASR,
    non per l'audio finale) a 16kHz, formato richiesto da faster-whisper.
    Evita una dipendenza aggiuntiva da scipy/librosa solo per questo."""
    if sr == target_sr:
        return audio_np.astype(np.float32)
    duration = len(audio_np) / sr
    n_target = max(1, int(round(duration * target_sr)))
    x_old = np.linspace(0, duration, num=len(audio_np), endpoint=False)
    x_new = np.linspace(0, duration, num=n_target, endpoint=False)
    return np.interp(x_new, x_old, audio_np).astype(np.float32)


def _load_asr_model(model_size, device):
    try:
        from faster_whisper import WhisperModel
    except ImportError:
        print("[ASR] 'faster-whisper' non installato. Installa con:\n"
              "  pip install faster-whisper --break-system-packages\n"
              "--asr-verify disattivato per questo run.", file=sys.stderr)
        return None
    compute_type = "int8" if device == "cpu" else "float16"
    return WhisperModel(model_size, device=device, compute_type=compute_type)


def _transcribe_segment(asr_model, audio_np, sr, language="it"):
    audio16k = _resample_for_asr(audio_np, sr)
    segments, _ = asr_model.transcribe(audio16k, language=language, beam_size=1,
                                        condition_on_previous_text=False)
    return " ".join(s.text for s in segments).strip()


def _normalize_words(text):
    text = text.lower()
    text = re.sub(r"[^\w\sàèéìòù']", " ", text)
    return text.split()


def _asr_check(transcribed_text, expected_text, lead_in_text, min_overlap):
    """Confronta la trascrizione ASR del segmento (dopo il trim) con il
    testo che avrebbe dovuto essere letto, per individuare due difetti che
    il rapporto durata/parole NON vede in modo affidabile (osservato
    all'ascolto ma non nel manifest): (1) residui del lead-in ripetuti
    nell'audio mantenuto - il trim automatico ha tagliato PRIMA di una
    ripetizione del lead-in invece che dopo -, e (2) parole del testo vero
    mangiate, alterate o sostituite senza un impatto sufficiente sulla
    durata totale del segmento da far scattare il flag basato sul rapporto.

    Ritorna (flag, dettaglio); flag e' None se non trova nulla di sospetto."""
    exp_words = _normalize_words(expected_text)
    got_words = _normalize_words(transcribed_text)
    if not exp_words:
        return None, ""
    if not got_words:
        return "sospetto_silenzio_asr", "ASR non ha trascritto nulla"

    lead_words = _normalize_words(lead_in_text) if lead_in_text else []
    if lead_words:
        max_n = min(len(lead_words), 4)
        window = " ".join(got_words[:max_n + 3])
        for n in range(max_n, 1, -1):
            phrase = " ".join(lead_words[-n:])
            if phrase and phrase in window:
                return ("sospetto_ripetizione_leadin",
                        f"'{phrase}' ripetuto in testa al segmento (dopo il trim)")

    overlap = difflib.SequenceMatcher(a=exp_words, b=got_words).ratio()
    if overlap < min_overlap:
        return ("sospetto_testo_errato",
                f"overlap parole={overlap:.2f} (atteso: '{' '.join(exp_words)}' | "
                f"trascritto: '{' '.join(got_words)}')")
    return None, ""


def _apply_fade_in(audio_np, sr, fade_ms=15):
    """Applica una brevissima dissolvenza in ingresso (default 15ms) per
    eliminare il click udibile quando si taglia l'audio esattamente a
    meta' di un'onda (discontinuita' di ampiezza) - capita sempre dopo un
    trim netto, sia in modalita' 'auto' che 'fixed' del lead-in filler."""
    fade_samples = min(int(fade_ms / 1000 * sr), len(audio_np))
    if fade_samples <= 0:
        return audio_np
    audio_np = audio_np.copy()
    ramp = np.linspace(0.0, 1.0, fade_samples, dtype=np.float32)
    audio_np[:fade_samples] *= ramp
    return audio_np


def _generate_task(payload):
    """Usa il modello GIA' caricato da _worker_init (globale nel processo)."""
    idx, text, speed, frames_after_eos, lead_in_text, lead_in_trim_sec, lead_in_trim_mode, seed = payload

    # Riproducibilita': l'unica fonte di casualita' nella generazione e' il
    # rumore gaussiano pescato dal decoder flow-matching (torch.nn.init.normal_
    # / trunc_normal_), che consuma il generatore globale di PyTorch. Fissando
    # il seed qui, IMMEDIATAMENTE prima della chiamata, ogni segmento diventa
    # deterministico: stesso testo + stesso seed + stessi parametri = stesso
    # identico audio, sempre. Ogni processo worker e' altrimenti avviato con
    # uno stato RNG diverso preso dall'entropia di sistema - da qui la
    # variabilita' run-to-run osservata finora.
    if seed is not None:
        import torch
        torch.manual_seed(seed)

    # ESPERIMENTO filler iniziale: si antepone un testo "di riscaldamento"
    # prima del testo vero, nell'ipotesi che gli artefatti all'inizio del
    # segmento siano legati a come il modello "parte" un'utterance senza
    # contesto audio precedente. Il filler viene poi tagliato via dall'audio
    # generato (non deve mai finire nell'output).
    if lead_in_text:
        text_to_generate = f"{lead_in_text} {text}"
    else:
        text_to_generate = text

    audio = _MODEL.generate_audio(_VOICE_STATE, text_to_generate, frames_after_eos=frames_after_eos)
    audio_np = audio.numpy()
    sr = _MODEL.sample_rate

    if lead_in_text and lead_in_trim_sec > 0:
        original_len = len(audio_np)
        if lead_in_trim_mode == "auto":
            audio_np = _auto_trim_lead_in(audio_np, sr, lead_in_trim_sec)
        else:
            trim_samples = int(lead_in_trim_sec * sr)
            if trim_samples < len(audio_np):
                audio_np = audio_np[trim_samples:]
        # Se il trim (in qualunque modalita') supera l'intera durata
        # generata, si lascia l'audio intatto piuttosto che azzerarlo:
        # meglio un filler residuo udibile che perdere il segmento intero.

        if len(audio_np) < original_len:
            # Il taglio netto crea una discontinuita' di ampiezza udibile
            # come click - una brevissima dissolvenza in ingresso la elimina.
            audio_np = _apply_fade_in(audio_np, sr)

    audio_np, sr = apply_speed(audio_np, sr, speed)
    return idx, audio_np, sr, seed


def _asr_language_code(pocket_language):
    """Mappa il valore di --language (formato pocket_tts, es. 'italian_24l')
    al codice lingua ISO-639-1 richiesto da faster-whisper. None = lascia
    che whisper rilevi la lingua da solo (fallback per lingue non mappate)."""
    lang = pocket_language.lower()
    mapping = {
        "ital": "it", "engl": "en", "span": "es", "espa": "es",
        "fren": "fr", "franc": "fr", "germ": "de", "deutsch": "de",
    }
    for prefix, code in mapping.items():
        if lang.startswith(prefix):
            return code
    return None


def _flag_of(ratio, n_words, duration, median_ratio, flag_ratio_low, flag_ratio_high,
              min_words_for_ratio, abs_duration_cap_short):
    """Flag basato sul rapporto durata/parole (troncamento/allucinazione).
    Vedi le note storiche nei commenti di main() per il perche' della
    soglia assoluta sotto min_words_for_ratio."""
    if median_ratio is None:
        return "ok"
    if n_words < min_words_for_ratio:
        return "sospetto_allucinazione" if duration > abs_duration_cap_short else "ok"
    if ratio < median_ratio * flag_ratio_low:
        return "sospetto_troncamento"
    if ratio > median_ratio * flag_ratio_high:
        return "sospetto_allucinazione"
    return "ok"


def _segment_dbfs(audio_np):
    """Volume medio (RMS) del segmento in dBFS. Usato per individuare
    segmenti anomali di volume (troppo alti o troppo bassi rispetto alla
    mediana del file) - difetto osservato all'ascolto ma invisibile al
    rapporto durata/parole e alla trascrizione ASR, perche' entrambi non
    guardano il volume."""
    rms = float(np.sqrt(np.mean(np.square(audio_np, dtype=np.float64))))
    if rms <= 1e-9:
        return -120.0
    return 20.0 * np.log10(rms)


def _tail_abruptness(audio_np, sr, tail_ms=120, ref_ms=400):
    """Rapporto tra l'energia RMS dell'ultimissimo tratto del segmento
    (tail_ms) e quella del tratto appena precedente (ref_ms). Un finale
    naturale (respiro, decadimento della voce a fine frase) ha energia
    calante verso la fine, quindi questo rapporto e' basso; un taglio
    netto a meta' di un suono lascia energia alta fino all'ultimo
    campione, quindi il rapporto resta vicino o sopra 1. Ritorna None se
    il segmento e' troppo corto per la misura."""
    tail_n = int(tail_ms / 1000 * sr)
    ref_n = int(ref_ms / 1000 * sr)
    if len(audio_np) < tail_n + ref_n:
        return None
    tail = audio_np[-tail_n:]
    ref = audio_np[-(tail_n + ref_n):-tail_n]
    tail_rms = float(np.sqrt(np.mean(np.square(tail, dtype=np.float64)))) + 1e-9
    ref_rms = float(np.sqrt(np.mean(np.square(ref, dtype=np.float64)))) + 1e-9
    return tail_rms / ref_rms


def _evaluate_segment(content, lead_in, audio_np, sr, median_ratio, median_dbfs, args, asr_model):
    """Valuta un segmento generato applicando in cascata tutti i controlli
    automatici disponibili, dal piu' al meno specifico: (1) rapporto
    durata/parole - troncamento/allucinazione grossolana; (2) trascrizione
    ASR (se --asr-verify) - testo diverso da quello atteso, incluse le
    ripetizioni del lead-in; (3) volume anomalo rispetto alla mediana del
    file; (4) finale tagliato bruscamente invece che sfumato. Si ferma al
    primo controllo che trova un problema (un segmento puo' averne piu' di
    uno, ma per decidere se rigenerare basta il primo).

    IMPORTANTE: questa cascata resta uno strumento di TRIAGE, non un
    sostituto dell'ascolto - non giudica naturalezza del tono, prosodia,
    o "sembra strano" in generale, solo le anomalie misurabili elencate
    sopra.

    Ritorna (flag, dettaglio, ratio, duration)."""
    n_words = len(content.split())
    duration = len(audio_np) / sr
    ratio = (duration / n_words) if n_words else 0.0

    flag = _flag_of(ratio, n_words, duration, median_ratio,
                     args.flag_ratio_low, args.flag_ratio_high,
                     args.min_words_for_ratio, args.abs_duration_cap_short)
    if flag != "ok":
        return flag, "", ratio, duration

    if asr_model is not None:
        transcribed = _transcribe_segment(asr_model, audio_np, sr, language=_asr_language_code(args.language))
        asr_flag, asr_detail = _asr_check(transcribed, content, lead_in, args.asr_min_overlap)
        if asr_flag is not None:
            return asr_flag, asr_detail, ratio, duration

    if not args.disable_volume_check and median_dbfs is not None:
        dbfs = _segment_dbfs(audio_np)
        if abs(dbfs - median_dbfs) > args.volume_anomaly_db:
            return ("sospetto_volume_anomalo",
                    f"dBFS={dbfs:.1f} (mediana file={median_dbfs:.1f})", ratio, duration)

    # DISATTIVATO DI DEFAULT (vedi --tail-check): validato solo su un tono
    # sintetico con fade artificiale, non su parlato reale. Nei test reali
    # (manifest del 2026-07-22) ha flaggato quasi TUTTI i segmenti - anche
    # quelli confermati "ok" all'ascolto in run precedenti - perche' il
    # parlato normale, specie su finali con consonanti o punteggiatura
    # "?"/"!", spesso NON ha un decadimento di energia graduale
    # nell'ultimo tratto: si ferma semplicemente li', senza che questo sia
    # un difetto. L'assunzione alla base della metrica (finale naturale =
    # energia calante) era sbagliata per il caso generale. Riabilitalo con
    # --tail-check solo se vuoi comunque provarlo, ma aspettati di doverne
    # rialzare parecchio la soglia (--tail-abrupt-ratio) rispetto al
    # default per non flaggare quasi tutto.
    if args.tail_check:
        tail_ratio = _tail_abruptness(audio_np, sr)
        if tail_ratio is not None and tail_ratio > args.tail_abrupt_ratio:
            return ("sospetto_taglio_finale_brusco",
                    f"rapporto energia coda/riferimento={tail_ratio:.2f}", ratio, duration)

    return "ok", "", ratio, duration


# ----------------------------------------------------------------------
# Main
# ----------------------------------------------------------------------

def _split_text_for_rescue(content):
    """Divide il testo di un segmento ancora rotto dopo tutti i retry in due
    parti piu' corte, spezzando alla virgola piu' vicina al centro (o, se
    non ce ne sono, a meta' delle parole). Ultima spiaggia per i pochi
    segmenti che il modello continua a non generare correttamente nonostante
    retry/rescue: due generazioni brevi e indipendenti hanno statisticamente
    meno probabilita' di fallire entrambe rispetto a una lunga.

    Ritorna (parte1, parte2) o None se il testo e' troppo corto (<4 parole)
    per essere spezzato in modo utile."""
    words = content.split()
    if len(words) < 4:
        return None

    comma_positions = [i for i, w in enumerate(words[:-1]) if w.endswith(",")]
    if comma_positions:
        mid = len(words) / 2
        split_at = min(comma_positions, key=lambda i: abs((i + 1) - mid)) + 1
    else:
        split_at = len(words) // 2

    part1 = " ".join(words[:split_at]).rstrip(",")
    part2 = " ".join(words[split_at:])
    if not part1 or not part2:
        return None
    return _ensure_terminal_punctuation(part1), part2


def _distribute_workers(groups, total_budget):
    """Distribuisce un budget TOTALE di worker tra i gruppi (per
    temperatura), proporzionalmente al numero di segmenti in ciascuno, con
    almeno 1 worker a gruppo e senza superare total_budget in totale.

    Importante: ogni worker carica una copia completa del modello in
    memoria. Un budget assegnato "per gruppo" invece che "in totale" puo'
    esaurire la memoria/il file di paging del sistema quando ci sono piu'
    gruppi attivi insieme (es. testo con temperature diverse via markup
    $/£, che durante un retry/salvataggio finiscono per caricare N modelli
    per M gruppi = N*M processi invece di N). Visto in un run reale su
    Windows: OSError 1455, 'file di paging troppo piccolo'."""
    n_groups = len(groups) if groups else 1
    remaining_budget = max(total_budget, n_groups)
    return {temp: max(1, min(len(indices), remaining_budget // n_groups))
            for temp, indices in groups.items()}


def main():
    parser = argparse.ArgumentParser(description="Pocket TTS espressivo, markup leggero + parallelismo sicuro per temperatura")
    parser.add_argument("input_txt")
    parser.add_argument("output_wav")
    parser.add_argument("--language", default="italian_24l")
    parser.add_argument("--voice", default="giovanni")
    parser.add_argument("--base-temperature", type=float, default=0.7)
    parser.add_argument("--base-speed", type=float, default=1.0)
    parser.add_argument("--max-words", type=int, default=18,
                         help="Numero massimo di parole per segmento generato (default 18). "
                              "Frasi piu' lunghe vengono spezzate su virgole/due punti/punto "
                              "e virgola. I test hanno mostrato che oltre le 20-25 parole "
                              "aumentano nettamente allucinazioni, parole saltate e picchi "
                              "di volume incontrollati.")
    parser.add_argument("--quantize", action="store_true")
    parser.add_argument("--eos-threshold", type=float, default=-3.0)
    parser.add_argument("--frames-after-eos", type=int, default=8,
                         help="Frame extra dopo EOS, 80ms/frame (default 8 = ~0.6s). "
                              "Valori piu' alti riducono il rischio di troncamento ma "
                              "aumentano il rischio di code allucinate dopo il testo vero.")
    parser.add_argument("--lsd-decode-steps", type=int, default=1,
                         help="Step di decodifica per frame (default 1 = piu' veloce). "
                              "Valori piu' alti (es. 3) possono migliorare stabilita' e "
                              "qualita', riducendo le allucinazioni, a costo di velocita'.")
    parser.add_argument("--workers", type=int, default=4,
                         help="Budget TOTALE di processi worker, distribuiti tra le "
                              "temperature distinte usate nel testo (default 4, sicuro "
                              "per la memoria). Sale con cautela: 6-8 e' un buon secondo "
                              "passo, evita di tornare a 12.")
    parser.add_argument("--debug", action="store_true",
                         help="Salva ogni segmento come file wav separato in una "
                              "sottocartella '<output>_debug/', insieme a un manifest.csv "
                              "con indice, temperatura, velocita', testo e durata di ciascuno. "
                              "Utile per isolare esattamente quali frasi causano problemi.")
    parser.add_argument("--lead-in-mode", choices=["context", "static", "off"], default="context",
                         help="Come costruire il testo 'di riscaldamento' anteposto a ogni "
                              "segmento prima della generazione (poi tagliato via, vedi "
                              "--lead-in-trim-sec) per attenuare la sillaba/parola iniziale "
                              "mangiata o frettolosa. 'context' (default, NUOVO): usa le "
                              "ultime --lead-in-context-words parole del segmento che, nel "
                              "testo originale, precedeva davvero quello corrente - contesto "
                              "linguistico vero invece di un filler arbitrario, utile anche "
                              "per il taglio automatico (il confine e' un vero confine di "
                              "frase/clausola). 'static': filler fisso identico per tutti i "
                              "segmenti, dato da --lead-in-text (comportamento delle versioni "
                              "precedenti). 'off': nessun lead-in.")
    parser.add_argument("--lead-in-text", default="",
                         help="Testo filler fisso usato con --lead-in-mode static, e come "
                              "fallback per il primissimo segmento del file in modalita' "
                              "'context' se --lead-in-fallback non e' impostato. Esempio: "
                              "'Allora,'")
    parser.add_argument("--lead-in-fallback", default="Allora,",
                         help="Filler usato SOLO per il primissimo segmento di testo del "
                              "file in modalita' 'context' (non ha un segmento precedente da "
                              "cui prendere contesto). Default: 'Allora,'")
    parser.add_argument("--lead-in-context-words", type=int, default=2,
                         help="In modalita' 'context': numero di parole finali del segmento "
                              "precedente usate come lead-in (default 2). Valori piu' bassi "
                              "riducono il rischio che il modello 'ripeta' quelle parole "
                              "invece di continuare con il testo vero (artefatto osservato "
                              "nei test con 4 parole) - a scapito di un contesto un po' meno "
                              "'caldo' per il modello.")
    parser.add_argument("--asr-verify", action="store_true",
                         help="Verifica ogni segmento generato trascrivendolo con un modello "
                              "ASR locale (faster-whisper) e confrontando il testo trascritto "
                              "con quello atteso. Individua difetti che il rapporto "
                              "durata/parole NON vede in modo affidabile: residui del lead-in "
                              "ripetuti nell'audio tenuto dopo il trim, parole mangiate o "
                              "sostituite senza impatto sufficiente sulla durata totale. "
                              "Richiede: pip install faster-whisper --break-system-packages. "
                              "Aggiunge tempo di elaborazione (trascrizione di ogni segmento "
                              "in modo seriale nel processo principale).")
    parser.add_argument("--asr-model-size", default="tiny",
                         help="Modello faster-whisper da usare per --asr-verify (default "
                              "'tiny', veloce su CPU; 'base' o 'small' sono piu' precisi ma "
                              "piu' lenti).")
    parser.add_argument("--asr-device", default="cpu",
                         help="Device per il modello ASR di --asr-verify (default 'cpu').")
    parser.add_argument("--asr-min-overlap", type=float, default=0.55,
                         help="Soglia (0-1, default 0.55) di sovrapposizione parole tra testo "
                              "atteso e trascritto sotto la quale un segmento viene flaggato "
                              "'sospetto_testo_errato' con --asr-verify.")
    parser.add_argument("--disable-volume-check", action="store_true",
                         help="Disattiva il controllo del volume anomalo (vedi "
                              "--volume-anomaly-db). Attivo di default.")
    parser.add_argument("--tail-check", action="store_true",
                         help="Attiva il controllo (sperimentale, DISATTIVATO di default) del "
                              "finale tagliato di netto - vedi --tail-abrupt-ratio. Nei test "
                              "reali ha flaggato quasi tutti i segmenti, anche quelli corretti: "
                              "il parlato normale spesso non ha un decadimento di energia "
                              "graduale nell'ultimo tratto. Attivalo solo se vuoi sperimentare, "
                              "e aspettati di dover alzare parecchio la soglia di default.")
    parser.add_argument("--volume-anomaly-db", type=float, default=4.0,
                         help="Un segmento il cui volume medio (dBFS) si discosta dalla "
                              "mediana del file di piu' di questa soglia in dB (default 4.0) "
                              "viene flaggato 'sospetto_volume_anomalo'. Cattura i casi di "
                              "volume anormalmente alto o basso segnalati all'ascolto ma "
                              "invisibili al rapporto durata/parole.")
    parser.add_argument("--tail-abrupt-ratio", type=float, default=0.82,
                         help="Un segmento viene flaggato 'sospetto_taglio_finale_brusco' se "
                              "il rapporto tra l'energia degli ultimi ~120ms e quella dei "
                              "~400ms precedenti supera questa soglia (default 0.82) - un "
                              "finale naturale ha energia calante (rapporto basso), un taglio "
                              "netto a meta' suono lascia energia alta fino alla fine. Soglia "
                              "euristica: se noti troppi falsi positivi/negativi nel manifest, "
                              "aggiustala confrontando la colonna del dettaglio con l'ascolto.")
    parser.add_argument("--rescue-attempts", type=int, default=2,
                         help="Dopo il retry normale (--max-retries, stessi parametri e seed "
                              "diverso), i segmenti ancora sospetti passano a un 'salvataggio' "
                              "con parametri via via piu' conservativi: ad ogni tentativo "
                              "aumenta --lsd-decode-steps e diminuisce la temperatura (vedi "
                              "--rescue-temp-step). Default 2 tentativi extra, 0 per "
                              "disattivare.")
    parser.add_argument("--rescue-temp-step", type=float, default=0.1,
                         help="Quanto abbassare la temperatura ad ogni tentativo di "
                              "salvataggio (default 0.1: il tentativo N usa temp originale - "
                              "N*0.1, con minimo 0.1).")
    parser.add_argument("--rescue-split", action="store_true", default=True,
                         help="Se un segmento resta sospetto anche dopo tutti i tentativi di "
                              "salvataggio, come ultima spiaggia lo spezza in due frasi piu' "
                              "corte generate separatamente e le concatena (due generazioni "
                              "brevi hanno meno probabilita' di fallire entrambe). Attivo di "
                              "default; usa --no-rescue-split per disattivarlo. Il risultato "
                              "e' marcato 'risolto_con_split' nel manifest - vale la pena "
                              "un ascolto di verifica sullo stacco tra le due parti.")
    parser.add_argument("--no-rescue-split", action="store_false", dest="rescue_split",
                         help="Disattiva --rescue-split.")
    parser.add_argument("--volume-fix", action="store_true", default=True,
                         help="Se un segmento resta 'sospetto_volume_anomalo' anche dopo "
                              "retry e salvataggio, corregge il guadagno in post-produzione "
                              "per riportarlo alla dBFS mediana del file (max +-12dB), invece "
                              "di continuare a rigenerare. Utile quando il modello legge "
                              "sistematicamente piu' piano/forte una frase specifica a "
                              "prescindere dal seed. Attivo di default; marcato "
                              "'risolto_con_correzione_volume' nel manifest.")
    parser.add_argument("--no-volume-fix", action="store_false", dest="volume_fix",
                         help="Disattiva --volume-fix.")
    parser.add_argument("--lead-in-trim-sec", type=float, default=0.4,
                         help="In modalita' 'fixed' (--lead-in-trim-mode): secondi da "
                              "tagliare dall'inizio dell'audio generato per rimuovere il "
                              "filler di --lead-in-text (default 0.4). In modalita' 'auto' "
                              "(default), usato solo come rete di sicurezza se non si trova "
                              "nessuna pausa naturale da rilevare.")
    parser.add_argument("--lead-in-trim-mode", choices=["auto", "fixed"], default="auto",
                         help="Come tagliare via il filler di --lead-in-text dall'audio "
                              "generato. 'auto' (default): rileva la prima pausa/gap di "
                              "silenzio nei primi ~2s e taglia li' - si adatta alla durata "
                              "reale del filler in ogni singolo segmento, invece di un numero "
                              "fisso indovinato a priori (che nei test lasciava residui "
                              "quando la durata variava). 'fixed': comportamento precedente, "
                              "taglia sempre --lead-in-trim-sec secondi.")
    parser.add_argument("--flag-ratio-low", type=float, default=0.6,
                         help="Soglia (frazione del rapporto sec/parola mediano del file, "
                              "default 0.6) sotto la quale un segmento viene flaggato come "
                              "'sospetto_troncamento' nel manifest di debug.")
    parser.add_argument("--flag-ratio-high", type=float, default=1.6,
                         help="Soglia (frazione del rapporto sec/parola mediano del file, "
                              "default 1.6) sopra la quale un segmento viene flaggato come "
                              "'sospetto_allucinazione' nel manifest di debug.")
    parser.add_argument("--ellipsis-pause-sec", type=float, default=0.45,
                         help="I puntini di sospensione ('...') nel testo vengono convertiti "
                              "in una pausa esplicita di questa durata in secondi (default "
                              "0.45), invece di lasciarli all'interpretazione del modello - "
                              "nei test la durata della pausa 'letta' dal modello per i puntini "
                              "letterali variava troppo (da <1s a diversi secondi) a parita' di "
                              "testo. Usa 0 per disattivare e tornare al comportamento precedente.")
    parser.add_argument("--seed-override", type=str, default="",
                         help="Forza un seed specifico per uno o piu' segmenti puntuali, "
                              "indipendentemente da --seed (funziona anche se --seed non e' "
                              "impostato). Formato: 'indice:seed,indice:seed' (es. "
                              "'6:12345,8:999'), dove 'indice' e' la colonna 'indice' del "
                              "manifest.csv. Utile per fissare/riprodurre esattamente un "
                              "segmento che ti ha convinto in un run precedente.")
    parser.add_argument("--only-segments", type=str, default="",
                         help="Rigenera SOLO gli indici di segmento elencati (colonna "
                              "'indice' nel manifest.csv), invece di tutto il capitolo. "
                              "Formato: lista separata da virgole, range 'a-b' supportati "
                              "(es. '6,8-10'). Richiede --reuse-debug-dir per recuperare "
                              "l'audio dei segmenti NON elencati da un run precedente fatto "
                              "con --debug; senza, lo script si ferma con un errore invece "
                              "di produrre un file con dei 'buchi'.")
    parser.add_argument("--reuse-debug-dir", type=str, default="",
                         help="Cartella '<output>_debug' di un run precedente (creata con "
                              "--debug) da cui recuperare l'audio dei segmenti non elencati "
                              "in --only-segments, cosi' puoi rigenerare a comando solo "
                              "alcuni segmenti (es. dopo un --seed-override) senza rilanciare "
                              "l'intero capitolo.")
    parser.add_argument("--seed", type=int, default=None,
                         help="Seed base per rendere la generazione deterministica e "
                              "riproducibile (default: nessuno, comportamento stocastico "
                              "come prima). L'unica fonte di casualita' del modello e' il "
                              "rumore gaussiano pescato dal decoder ad ogni frame; fissando "
                              "il seed qui, stesso testo + stesso seed + stessi parametri = "
                              "stesso audio identico ad ogni run. Ogni segmento usa un seed "
                              "derivato (seed + indice) cosi' i segmenti non sono tutti "
                              "identici tra loro; ogni tentativo di retry usa un seed derivato "
                              "diverso dall'originale, cosi' il retry puo' comunque dare un "
                              "risultato diverso da tenere/scartare (vedi manifest, colonna "
                              "'seed', per riprodurre esattamente un segmento specifico in futuro).")
    parser.add_argument("--min-words-for-ratio", type=int, default=3,
                         help="Numero minimo di parole di un segmento sotto il quale il "
                              "rapporto sec/parola NON viene usato per il flag (default 3): "
                              "su segmenti cosi' brevi l'overhead fisso per segmento (frame "
                              "dopo EOS, trim lead-in, micro-silenzi) domina il rapporto "
                              "indipendentemente dal contenuto, causando falsi positivi/negativi. "
                              "Sotto questa soglia si usa invece --abs-duration-cap-short.")
    parser.add_argument("--abs-duration-cap-short", type=float, default=3.0,
                         help="Durata assoluta in secondi (default 3.0) sopra la quale un "
                              "segmento con meno di --min-words-for-ratio parole viene flaggato "
                              "come 'sospetto_allucinazione'. Sotto questa durata, un segmento "
                              "breve viene considerato 'ok' anche se il rapporto sec/parola "
                              "relativo sarebbe fuori soglia (rumore statistico su n piccolo).")
    parser.add_argument("--max-retries", type=int, default=1,
                         help="Numero di tentativi extra di generazione automatica per i "
                              "segmenti flaggati come 'sospetto_troncamento' o "
                              "'sospetto_allucinazione' in base al rapporto sec/parola "
                              "(default 1). Ad ogni tentativo si tiene il risultato con "
                              "rapporto piu' vicino alla mediana del file. Metti 0 per "
                              "disattivare e tornare al comportamento precedente (nessun "
                              "retry, un solo tentativo per segmento).")
    args = parser.parse_args()

    input_path = Path(args.input_txt)
    text = None
    for encoding in ("utf-8", "utf-8-sig", "cp1252", "latin-1"):
        try:
            text = input_path.read_text(encoding=encoding)
            if encoding != "utf-8":
                print(f"[avviso] File letto con codifica '{encoding}' "
                      f"(non era UTF-8 puro). Se noti caratteri strani "
                      f"nell'audio, salva il .txt come UTF-8 dall'editor.",
                      file=sys.stderr)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        print(f"Impossibile leggere '{args.input_txt}': "
              f"non e' un file di testo valido (forse hai trascinato "
              f"un file audio o binario per errore?).", file=sys.stderr)
        sys.exit(1)
    segments = parse_markup(text, args.base_temperature, args.base_speed, args.max_words, args.ellipsis_pause_sec)

    if not segments:
        print("Nessun segmento trovato.", file=sys.stderr)
        sys.exit(1)

    _assign_lead_ins(segments, args.lead_in_mode, args.lead_in_text,
                      args.lead_in_fallback, args.lead_in_context_words)

    seed_overrides = _parse_seed_overrides(args.seed_override)
    only_segments = _parse_idx_list(args.only_segments)

    # Raggruppa gli indici di testo per temperatura (arrotondata a 1 decimale).
    # Se --only-segments e' impostato, i segmenti NON elencati vengono esclusi
    # dalla generazione: il loro audio verra' recuperato da --reuse-debug-dir
    # invece di essere rigenerato (vedi piu' sotto).
    groups = defaultdict(list)
    reused_indices = []
    for i, seg in enumerate(segments):
        if seg["type"] != "text":
            continue
        if only_segments is not None and i not in only_segments:
            reused_indices.append(i)
            continue
        groups[round(seg["temp"], 1)].append(i)

    n_groups = len(groups) if groups else 1
    n_to_generate = sum(len(v) for v in groups.values())
    if only_segments is not None:
        print(f"Segmenti di testo: {n_to_generate + len(reused_indices)} totali | "
              f"da rigenerare: {n_to_generate} {sorted(only_segments)} | "
              f"da riusare: {len(reused_indices)} | budget worker: {args.workers}",
              file=sys.stderr)
    else:
        print(f"Segmenti di testo: {n_to_generate} | "
              f"temperature distinte: {n_groups} | budget worker: {args.workers}",
              file=sys.stderr)
    if args.lead_in_mode != "off":
        print(f"[LEAD-IN] Modalita' '{args.lead_in_mode}' attiva "
              f"(trim: {args.lead_in_trim_mode}, fallback trim: {args.lead_in_trim_sec}s)"
              + (f" — filler fisso: \"{args.lead_in_text}\"" if args.lead_in_mode == "static" else
                 f" — {args.lead_in_context_words} parole di contesto dal segmento precedente, "
                 f"fallback 1° segmento: \"{args.lead_in_fallback}\"")
              + " — ricordalo nel nome del file di output/manifest.",
              file=sys.stderr)

    # Distribuisce il budget di worker tra i gruppi, proporzionalmente al
    # numero di segmenti in ciascun gruppo, con almeno 1 worker a gruppo.
    workers_per_group = {}
    remaining_budget = max(args.workers, n_groups)  # almeno 1 worker a gruppo
    for temp, indices in groups.items():
        w = max(1, min(len(indices), remaining_budget // n_groups))
        workers_per_group[temp] = w

    total_workers = sum(workers_per_group.values())
    print(f"Distribuzione worker per temperatura: {workers_per_group} "
          f"(totale processi: {total_workers})", file=sys.stderr)

    results = {}

    if reused_indices:
        if not args.reuse_debug_dir:
            print("Errore: --only-segments richiede anche --reuse-debug-dir (la cartella "
                  "'<output>_debug' di un run precedente fatto con --debug), da cui "
                  "recuperare l'audio dei segmenti non elencati. Senza, il file finale "
                  "avrebbe dei 'buchi' per i segmenti non rigenerati.", file=sys.stderr)
            sys.exit(1)
        for i in reused_indices:
            results[i] = _load_reused_segment(i, args.reuse_debug_dir)
        print(f"Riusato audio esistente per {len(reused_indices)} segmenti da "
              f"'{args.reuse_debug_dir}'.", file=sys.stderr)

    with ExitStack() as stack:
        futures = []
        for temp, indices in groups.items():
            n_workers = workers_per_group[temp]
            executor = stack.enter_context(ProcessPoolExecutor(
                max_workers=n_workers,
                initializer=_worker_init,
                initargs=(args.language, temp, args.quantize, args.eos_threshold, args.voice, args.lsd_decode_steps),
            ))
            for idx in indices:
                seg_seed = seed_overrides.get(idx, (args.seed + idx) if args.seed is not None else None)
                payload = (idx, segments[idx]["content"], segments[idx]["speed"], args.frames_after_eos,
                           segments[idx].get("lead_in", ""), args.lead_in_trim_sec, args.lead_in_trim_mode, seg_seed)
                futures.append(executor.submit(_generate_task, payload))

        done_count = 0
        total = len(futures)
        seeds_used = {i: seed_overrides.get(i) for i in reused_indices}
        for future in as_completed(futures):
            idx, audio_np, sr, used_seed = future.result()
            results[idx] = (audio_np, sr)
            seeds_used[idx] = used_seed
            done_count += 1
            print(f"[{done_count}/{total}] Segmento {idx} completato", file=sys.stderr)

    from pydub import AudioSegment
    from pydub.effects import normalize
    import statistics

    # ------------------------------------------------------------------
    # Calcolo SEMPRE rapporto sec/parola, dBFS mediano e flag di ogni
    # segmento di testo (non solo in modalita' --debug): serve anche al
    # retry automatico qui sotto, che deve poter funzionare anche senza
    # --debug. La scrittura di manifest.csv + wav singoli resta invece
    # condizionata a --debug (piu' sotto).
    #
    # NOTA IMPORTANTE (dall'ascolto dei run precedenti): il rapporto
    # durata/parole e la trascrizione ASR non vedono difetti puramente
    # "acustici" - volume anomalo di un intero segmento, o un finale
    # tagliato di netto invece che sfumato - perche' il testo trascritto
    # puo' essere comunque corretto. Per questo motivo la valutazione di
    # un segmento (vedi _evaluate_segment) e' una CASCATA di controlli
    # via via meno specifici: durata -> ASR -> volume -> brusquezza del
    # finale. Nessuna di queste automazioni sostituisce un ascolto finale
    # su un campione del capitolo, ma restringono di molto quanto resta
    # da controllare a mano.
    # ------------------------------------------------------------------
    text_indices = [i for i, seg in enumerate(segments) if seg["type"] == "text"]

    def _ratio_of(idx):
        audio_np, sr = results[idx]
        n_words = len(segments[idx]["content"].split())
        return ((len(audio_np) / sr) / n_words) if n_words else 0.0

    ratios = {i: _ratio_of(i) for i in text_indices}
    median_ratio = statistics.median(ratios.values()) if ratios else None

    dbfs_vals = {i: _segment_dbfs(results[i][0]) for i in text_indices}
    median_dbfs = statistics.median(dbfs_vals.values()) if dbfs_vals else None

    asr_model = _load_asr_model(args.asr_model_size, args.asr_device) if args.asr_verify else None
    if args.asr_verify and asr_model is not None:
        print(f"\n[ASR] Verifica trascrizione di {len(text_indices)} segmenti "
              f"(modello '{args.asr_model_size}', device '{args.asr_device}', "
              f"lingua '{_asr_language_code(args.language) or 'auto'}')...", file=sys.stderr)

    flags = {}
    details = {}
    for i in text_indices:
        audio_np, sr = results[i]
        flag, detail, ratio, _duration = _evaluate_segment(
            segments[i]["content"], segments[i].get("lead_in", ""), audio_np, sr,
            median_ratio, median_dbfs, args, asr_model)
        flags[i] = flag
        details[i] = detail
        ratios[i] = ratio
        if flag != "ok":
            print(f"[CHECK] Segmento {i}: {flag}" + (f" - {detail}" if detail else ""), file=sys.stderr)

    attempts_used = {i: 0 for i in text_indices}

    # IMPORTANTE: se --only-segments e' attivo, i segmenti riusati da
    # --reuse-debug-dir (reused_indices) NON vanno rimessi in coda per
    # retry/salvataggio anche se il loro flag risulta "sospetto" - non
    # sono stati generati in questo run, e l'obiettivo di --only-segments
    # e' toccare SOLO gli indici richiesti. Il loro flag viene comunque
    # calcolato e mostrato nel manifest (informativo, riflette lo stato
    # gia' presente nella cartella di debug riusata), ma restano esclusi
    # dalla coda di retry qui sotto.
    reused_set = set(reused_indices)
    flagged_indices = [i for i in text_indices if flags[i] != "ok" and i not in reused_set]
    n_reused_still_flagged = sum(1 for i in reused_indices if flags.get(i, "ok") != "ok")
    if n_reused_still_flagged:
        print(f"[INFO] {n_reused_still_flagged} segmenti riusati da --reuse-debug-dir "
              f"risultano ancora 'sospetti' nel manifest, ma non vengono ritoccati: non "
              f"sono stati generati in questo run (--only-segments li esclude di proposito). "
              f"Se vuoi sistemarli, rilancia con quell'indice in --only-segments.",
              file=sys.stderr)

    # ------------------------------------------------------------------
    # RETRY AUTOMATICO: per ogni segmento flaggato, rigenera fino a
    # --max-retries volte in piu'. Tiene il tentativo che passa la
    # cascata di controlli (o, se nessuno dei due passa/nessuno fallisce
    # diversamente dall'altro, quello con rapporto piu' vicino alla
    # mediana del file). Automatizza esattamente il ciclo manuale
    # "ascolta -> rilancia -> confronta" fatto finora a mano.
    # ------------------------------------------------------------------
    if flagged_indices and args.max_retries > 0:
        print(f"\n[RETRY] {len(flagged_indices)} segmenti flaggati {flagged_indices} - "
              f"tentativo automatico di rigenerazione (max {args.max_retries} "
              f"tentativi extra ciascuno)...", file=sys.stderr)

        retry_groups = defaultdict(list)
        for i in flagged_indices:
            retry_groups[round(segments[i]["temp"], 1)].append(i)

        n_improved = 0
        with ExitStack() as retry_stack:
            retry_executors = {}
            retry_workers = _distribute_workers(retry_groups, args.workers)
            for temp, indices in retry_groups.items():
                n_workers = retry_workers[temp]
                retry_executors[temp] = retry_stack.enter_context(ProcessPoolExecutor(
                    max_workers=n_workers,
                    initializer=_worker_init,
                    initargs=(args.language, temp, args.quantize, args.eos_threshold,
                              args.voice, args.lsd_decode_steps),
                ))

            for attempt_n in range(1, args.max_retries + 1):
                still_flagged = [i for i in flagged_indices if flags[i] != "ok"]
                if not still_flagged:
                    break

                retry_futures = []
                for i in still_flagged:
                    temp = round(segments[i]["temp"], 1)
                    # Seed diverso per ogni tentativo di retry (offset grande per
                    # non collidere con l'originale o con altri indici/tentativi),
                    # cosi' il retry puo' comunque produrre un risultato diverso
                    # da confrontare - ma resta comunque riproducibile una volta
                    # scelto (vedi colonna 'seed' nel manifest).
                    seg_seed = (args.seed + i + attempt_n * 100_000) if args.seed is not None else None
                    payload = (i, segments[i]["content"], segments[i]["speed"], args.frames_after_eos,
                               segments[i].get("lead_in", ""), args.lead_in_trim_sec, args.lead_in_trim_mode, seg_seed)
                    retry_futures.append(retry_executors[temp].submit(_generate_task, payload))

                for future in as_completed(retry_futures):
                    idx, audio_np, sr, used_seed = future.result()
                    attempts_used[idx] += 1
                    new_flag, new_detail, new_ratio, _new_duration = _evaluate_segment(
                        segments[idx]["content"], segments[idx].get("lead_in", ""), audio_np, sr,
                        median_ratio, median_dbfs, args, asr_model)

                    current_dist = abs(ratios[idx] - median_ratio) if median_ratio else 0.0
                    new_dist = abs(new_ratio - median_ratio) if median_ratio else 0.0
                    old_ok = flags[idx] == "ok"
                    new_ok = new_flag == "ok"
                    # Se uno dei due tentativi passa la cascata di controlli e
                    # l'altro no, vince quello che passa - indipendentemente da
                    # quale ha il rapporto durata piu' "tipico". Solo se
                    # entrambi passano o entrambi falliscono si ricade sul
                    # confronto per distanza dalla mediana.
                    kept = new_ok if old_ok != new_ok else (new_dist < current_dist)

                    if kept:
                        results[idx] = (audio_np, sr)
                        ratios[idx] = new_ratio
                        flags[idx] = new_flag
                        details[idx] = new_detail
                        seeds_used[idx] = used_seed
                        if new_flag == "ok":
                            n_improved += 1

                    print(f"[RETRY {attempt_n}/{args.max_retries}] Segmento {idx}: '{new_flag}'"
                          + (f" - {new_detail}" if new_detail else "")
                          + f" ({'tenuto' if kept else 'scartato, versione precedente migliore'})",
                          file=sys.stderr)

        n_still_flagged = sum(1 for i in flagged_indices if flags[i] != "ok")
        print(f"[RETRY] Esito: {n_improved}/{len(flagged_indices)} segmenti risolti. "
              f"{n_still_flagged} ancora sospetti dopo tutti i tentativi consentiti "
              f"- questi passano al salvataggio (parametri piu' conservativi).\n", file=sys.stderr)

    # ------------------------------------------------------------------
    # SALVATAGGIO: per i segmenti ancora sospetti dopo il retry normale
    # (stessi parametri, solo seed diverso) - tipicamente rumore puro o
    # frasi non lette/lette a meta', gia' viste su testi con vocabolario
    # insolito o punteggiatura particolare - un ultimo giro con parametri
    # via via piu' "conservativi": piu' step di decodifica
    # (--lsd-decode-steps, che riduce le instabilita' a scapito della
    # velocita') e temperatura piu' bassa (piu' deterministica). Se anche
    # questo non basta, ultima spiaggia: spezza il testo del segmento in
    # due frasi piu' corte e le genera separatamente - due generazioni
    # brevi hanno statisticamente meno probabilita' di fallire entrambe
    # rispetto a una lunga con vocabolario/struttura insolita.
    # ------------------------------------------------------------------
    still_broken = [i for i in text_indices if flags[i] != "ok" and i not in reused_set]
    if still_broken and args.rescue_attempts > 0:
        print(f"[SALVATAGGIO] {len(still_broken)} segmenti ancora sospetti {still_broken} - "
              f"tentativi con parametri piu' conservativi (max {args.rescue_attempts})...",
              file=sys.stderr)

        for rescue_n in range(1, args.rescue_attempts + 1):
            pending = [i for i in still_broken if flags[i] != "ok"]
            if not pending:
                break

            rescue_steps = min(args.lsd_decode_steps + rescue_n, 4)
            temp_delta = args.rescue_temp_step * rescue_n

            rescue_groups = defaultdict(list)
            for i in pending:
                rescue_temp = round(max(0.1, segments[i]["temp"] - temp_delta), 2)
                rescue_groups[rescue_temp].append(i)

            with ExitStack() as rescue_stack:
                rescue_executors = {}
                rescue_workers = _distribute_workers(rescue_groups, args.workers)
                for temp, indices in rescue_groups.items():
                    n_workers = rescue_workers[temp]
                    rescue_executors[temp] = rescue_stack.enter_context(ProcessPoolExecutor(
                        max_workers=n_workers,
                        initializer=_worker_init,
                        initargs=(args.language, temp, args.quantize, args.eos_threshold,
                                  args.voice, rescue_steps),
                    ))

                rescue_futures = []
                for temp, indices in rescue_groups.items():
                    for i in indices:
                        seg_seed = (args.seed + i + rescue_n * 1_000_000) if args.seed is not None else None
                        payload = (i, segments[i]["content"], segments[i]["speed"], args.frames_after_eos,
                                   segments[i].get("lead_in", ""), args.lead_in_trim_sec,
                                   args.lead_in_trim_mode, seg_seed)
                        rescue_futures.append((temp, rescue_executors[temp].submit(_generate_task, payload)))

                for temp, future in rescue_futures:
                    idx, audio_np, sr, used_seed = future.result()
                    attempts_used[idx] += 1
                    new_flag, new_detail, new_ratio, _d = _evaluate_segment(
                        segments[idx]["content"], segments[idx].get("lead_in", ""), audio_np, sr,
                        median_ratio, median_dbfs, args, asr_model)

                    old_ok = flags[idx] == "ok"
                    new_ok = new_flag == "ok"
                    current_dist = abs(ratios[idx] - median_ratio) if median_ratio else 0.0
                    new_dist = abs(new_ratio - median_ratio) if median_ratio else 0.0
                    kept = new_ok if old_ok != new_ok else (new_dist < current_dist)

                    if kept:
                        results[idx] = (audio_np, sr)
                        ratios[idx] = new_ratio
                        flags[idx] = new_flag
                        details[idx] = new_detail
                        seeds_used[idx] = used_seed

                    print(f"[SALVATAGGIO {rescue_n}/{args.rescue_attempts}] Segmento {idx} "
                          f"(temp={temp:.2f}, lsd_decode_steps={rescue_steps}): '{new_flag}'"
                          + (f" - {new_detail}" if new_detail else "")
                          + f" ({'tenuto' if kept else 'scartato'})", file=sys.stderr)

        # Ultima spiaggia: split in due generazioni piu' corte.
        still_hopeless = [i for i in still_broken if flags[i] != "ok"]
        if still_hopeless and args.rescue_split:
            print(f"[SALVATAGGIO] Ultimo tentativo, spezzo in due il testo di: {still_hopeless}",
                  file=sys.stderr)

            split_plan = {}
            split_groups = defaultdict(list)
            for i in still_hopeless:
                parts = _split_text_for_rescue(segments[i]["content"])
                if parts is None:
                    print(f"[SALVATAGGIO] Segmento {i}: troppo corto per essere spezzato, "
                          f"resta sospetto - richiede intervento manuale (riformulare la frase "
                          f"nel testo originale, o provare seed diversi con --seed-override).",
                          file=sys.stderr)
                    continue
                split_plan[i] = parts
                split_groups[round(segments[i]["temp"], 1)].append(i)

            if split_plan:
                split_steps = min(args.lsd_decode_steps + 1, 4)
                with ExitStack() as split_stack:
                    split_executors = {}
                    split_workers = _distribute_workers(split_groups, args.workers)
                    for temp, indices in split_groups.items():
                        n_workers = split_workers[temp]
                        split_executors[temp] = split_stack.enter_context(ProcessPoolExecutor(
                            max_workers=n_workers,
                            initializer=_worker_init,
                            initargs=(args.language, temp, args.quantize, args.eos_threshold,
                                      args.voice, split_steps),
                        ))

                    split_futures = {}
                    for temp, indices in split_groups.items():
                        for i in indices:
                            part1, part2 = split_plan[i]
                            seed1 = (args.seed + i + 4_000_000) if args.seed is not None else None
                            seed2 = (args.seed + i + 5_000_000) if args.seed is not None else None
                            lead_in_1 = segments[i].get("lead_in", "")
                            lead_in_2 = " ".join(part1.split()[-args.lead_in_context_words:])
                            f1 = split_executors[temp].submit(_generate_task, (
                                i, part1, segments[i]["speed"], args.frames_after_eos,
                                lead_in_1, args.lead_in_trim_sec, args.lead_in_trim_mode, seed1))
                            f2 = split_executors[temp].submit(_generate_task, (
                                i, part2, segments[i]["speed"], args.frames_after_eos,
                                lead_in_2, args.lead_in_trim_sec, args.lead_in_trim_mode, seed2))
                            split_futures[i] = (f1, f2)

                    for i, (f1, f2) in split_futures.items():
                        _, audio1, sr1, _ = f1.result()
                        _, audio2, sr2, _ = f2.result()
                        attempts_used[i] += 2
                        gap = np.zeros(int(0.12 * sr1), dtype=audio1.dtype)
                        combined = np.concatenate([audio1, gap, audio2])
                        new_flag, new_detail, new_ratio, _d = _evaluate_segment(
                            segments[i]["content"], segments[i].get("lead_in", ""), combined, sr1,
                            median_ratio, median_dbfs, args, asr_model)
                        results[i] = (combined, sr1)
                        ratios[i] = new_ratio
                        flags[i] = new_flag if new_flag != "ok" else "risolto_con_split"
                        details[i] = new_detail or "segmento diviso in due generazioni piu' corte - controlla lo stacco"
                        print(f"[SALVATAGGIO split] Segmento {i}: '{flags[i]}'"
                              + (f" - {details[i]}" if details[i] else ""), file=sys.stderr)

        n_still_hopeless = sum(1 for i in still_broken if flags[i] not in ("ok", "risolto_con_split"))
        print(f"[SALVATAGGIO] Esito: {n_still_hopeless}/{len(still_broken)} segmenti restano "
              f"sospetti dopo TUTTI i tentativi (retry + salvataggio) - questi vanno ascoltati "
              f"e sistemati a mano, magari riformulando la frase nel testo originale.\n",
              file=sys.stderr)

    # ------------------------------------------------------------------
    # CORREZIONE VOLUME (ultima rete, dopo retry/salvataggio): un
    # segmento rimasto 'sospetto_volume_anomalo' anche dopo tutti i
    # tentativi generativi non e' necessariamente un errore da rigenerare
    # all'infinito - a volte il modello legge sistematicamente piu' piano
    # (o piu' forte) quella frase specifica, indipendentemente dal seed.
    # In quel caso e' piu' efficace correggere il guadagno in post
    # (scalando l'ampiezza per riportare il segmento alla dBFS mediana
    # del file) che continuare a rigenerare. Attivo di default
    # (--no-volume-fix per disattivarlo); corregge solo cio' che resta
    # flaggato DOPO retry+salvataggio, non tocca segmenti "ok".
    # ------------------------------------------------------------------
    if args.volume_fix and median_dbfs is not None:
        for i in text_indices:
            if flags[i] != "sospetto_volume_anomalo" or i in reused_set:
                continue
            audio_np, sr = results[i]
            current_dbfs = _segment_dbfs(audio_np)
            gain_db = median_dbfs - current_dbfs
            gain_db = max(-12.0, min(12.0, gain_db))  # margine di sicurezza, evita correzioni estreme
            factor = 10.0 ** (gain_db / 20.0)
            corrected = np.clip(audio_np * factor, -1.0, 1.0)
            results[i] = (corrected, sr)
            new_dbfs = _segment_dbfs(corrected)
            flags[i] = "risolto_con_correzione_volume"
            details[i] = f"guadagno applicato: {gain_db:+.1f}dB (da {current_dbfs:.1f} a {new_dbfs:.1f} dBFS)"
            print(f"[VOLUME] Segmento {i}: guadagno {gain_db:+.1f}dB applicato "
                  f"({current_dbfs:.1f} -> {new_dbfs:.1f} dBFS)", file=sys.stderr)

    # Modalita' debug: salva ogni segmento come wav separato + manifest.csv
    # (riusa i flag/rapporti gia' calcolati sopra, POST-retry se applicato)
    if args.debug:
        import csv
        debug_dir = Path(args.output_wav).with_suffix("")
        debug_dir = debug_dir.parent / (debug_dir.name + "_debug")
        debug_dir.mkdir(parents=True, exist_ok=True)

        manifest_path = debug_dir / "manifest.csv"
        n_flagged = 0
        with open(manifest_path, "w", newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            writer.writerow(["indice", "tipo", "temp", "speed", "lsd_decode_steps",
                              "lead_in_usato", "lead_in_trim_sec",
                              "durata_sec", "n_parole", "sec_per_parola", "flag", "dettaglio",
                              "tentativi_retry", "seed", "file", "testo"])

            for i, seg in enumerate(segments):
                if seg["type"] == "pause":
                    writer.writerow([i, "pausa", "", "", args.lsd_decode_steps,
                                      "", args.lead_in_trim_sec,
                                      seg["duration"], "", "", "", "", "", "", "", ""])
                    continue

                audio_np, sr = results[i]
                duration_sec = len(audio_np) / sr
                fname = f"{i:03d}_T{seg['temp']}_S{seg['speed']}.wav"

                int16_audio = (audio_np * 32767).astype(np.int16)
                seg_audio = AudioSegment(int16_audio.tobytes(), frame_rate=sr, sample_width=2, channels=1)
                seg_audio = normalize(seg_audio, headroom=0.5)
                seg_audio.export(debug_dir / fname, format="wav")

                n_words = len(seg["content"].split())
                ratio = ratios[i]
                flag = flags[i]
                if flag not in ("ok", "risolto_con_split", "risolto_con_correzione_volume"):
                    n_flagged += 1

                writer.writerow([i, "testo", seg["temp"], seg["speed"], args.lsd_decode_steps,
                                  seg.get("lead_in", ""), args.lead_in_trim_sec,
                                  f"{duration_sec:.2f}", n_words, f"{ratio:.3f}", flag,
                                  details.get(i, ""),
                                  attempts_used[i], seeds_used.get(i, ""), fname, seg["content"]])

        print(f"\n[DEBUG] Segmenti singoli salvati in: {debug_dir}", file=sys.stderr)
        print(f"[DEBUG] Manifest: {manifest_path}", file=sys.stderr)
        if median_ratio:
            print(f"[DEBUG] Rapporto sec/parola mediano: {median_ratio:.3f} "
                  f"(soglie flag: <{median_ratio * args.flag_ratio_low:.3f} sospetto_troncamento, "
                  f">{median_ratio * args.flag_ratio_high:.3f} sospetto_allucinazione) — "
                  f"{n_flagged}/{len(text_indices)} segmenti ancora flaggati dopo il retry.",
                  file=sys.stderr)
        print("[DEBUG] Apri manifest.csv (es. con Excel) per vedere testo/temp/speed/durata/flag/tentativi di ogni file.\n",
              file=sys.stderr)

    final_audio = AudioSegment.empty()
    sample_rate_ref = 24000

    for i, seg in enumerate(segments):
        if seg["type"] == "pause":
            duration_ms = int(seg["duration"] * 1000)
            final_audio += AudioSegment.silent(duration=duration_ms, frame_rate=sample_rate_ref)
            continue

        audio_np, sr = results[i]
        sample_rate_ref = sr
        int16_audio = (audio_np * 32767).astype(np.int16)
        seg_audio = AudioSegment(int16_audio.tobytes(), frame_rate=sr, sample_width=2, channels=1)
        final_audio += seg_audio

    print("Normalizzo il volume ...", file=sys.stderr)
    final_audio = normalize(final_audio, headroom=0.5)

    final_audio.export(args.output_wav, format="wav")
    print(f"\nFatto! Audio salvato in: {args.output_wav}", file=sys.stderr)


if __name__ == "__main__":
    main()