# Pocket TTS Enhanced

Enhancements for [Kyutai's Pocket TTS](https://github.com/kyutai-labs/pocket-tts): inline markup for speed, pause and temperature control, plus an automated pipeline to detect and fix hallucinated or truncated audio segments, and a targeted tool to regenerate a single bad segment without redoing the whole file.

Language-agnostic in principle — the markup and QA pipeline don't depend on Italian, only the bundled `.bat` launchers default to an Italian voice/model as that's what this was built and tested with. Swap `--language` / `--voice` for any language Pocket TTS supports.

This project also happens to implement what the original repo lists under ["Unsupported features"](https://github.com/kyutai-labs/pocket-tts#unsupported-features): adding pauses to the text input (tracked as [issue #6](https://github.com/kyutai-labs/pocket-tts/issues/6)).

## What's in here

| File | What it does |
|---|---|
| `pocket_tts_expressive.py` | Main generator. Parses the markup, splits text into TTS-friendly chunks, generates audio in parallel, runs the QA/rescue pipeline, and (optionally) writes a debug manifest. |
| `genera_espressivo_debug.bat` | Windows launcher for a full run with `--debug` always on (drag a `.txt` file onto it, or run it and type the filename). |
| `rigenera_segmento.py` | Regenerates a single flagged segment (by index, from the debug manifest) and re-stitches the full WAV. |
| `rigenera_segmento_espressivo_v2.bat` | Windows launcher for `rigenera_segmento.py` — walks you through picking the segment index, temperature, speed and optional seed. |

## Prerequisites

1. Install Pocket TTS following the [official instructions](https://github.com/kyutai-labs/pocket-tts) (or `pip install pocket-tts`).
2. `pip install pydub` (the `.bat` files install this automatically via `uv pip install pydub`).
3. `ffmpeg` available on PATH (required by `pydub`).
4. Optional, only for `--asr-verify`: `pip install faster-whisper --break-system-packages`.
5. Place these files in the same folder as your Pocket TTS installation / virtual environment (the `.bat` files expect `.venv\Scripts\activate` to exist alongside them).

## Adapting this to your own setup

The `.bat` files and the two Python scripts were written for one specific folder layout and one specific voice/language. Before using them, check these spots:

- **`.venv` location**: both `.bat` files run `call .venv\Scripts\activate` — this assumes the virtual environment is a folder named `.venv` in the *same directory as the `.bat` file itself*. If your venv is named differently or lives elsewhere, edit that line in both `.bat` files (or move/rename your venv to match).
- **Script name/location**: `rigenera_segmento.py` calls the main generator with a relative path, `sys.executable, 'pocket_tts_expressive.py'` — if you keep all files in the same folder (as intended) this just works; if you rename or relocate `pocket_tts_expressive.py`, update that line accordingly.
- **Default language/voice**: hardcoded in both `.bat` files (`--language italian_24l --voice giovanni`) and, separately, in `rigenera_segmento.py` (same two flags, inside the `cmd = [...]` list). If you want a different default, change all three places — see the voice/language section below.

## Choosing a different voice or language

The defaults here (`italian_24l` / `giovanni`) are what this was built and tested with, but Pocket TTS ships more options and also supports voice cloning:

- **Pre-made voices**: Kyutai provides a small catalog of ready-to-use voices with their licenses listed on the [voice catalog page](https://huggingface.co/kyutai/tts-voices). Pass any of them with `--voice <name>`.
- **Cloning your own voice**: `--voice` also accepts a path to a local `.wav`/`.mp3` file, or an `hf://` path to a sample hosted on Hugging Face — Pocket TTS will clone that voice on the fly. See the [official generate/export-voice docs](https://kyutai-labs.github.io/pocket-tts/) for details, including the `export-voice` command that converts a cloned sample into a `.safetensors` file for much faster loading on repeated runs (useful if you clone a voice you'll reuse often).
- **Other languages**: pass `--language <code>` (see the official repo for the currently supported list — more are being added over time).

Once you've picked a voice/language, update `--voice` and `--language` in the `.bat` files and in `rigenera_segmento.py` as described above, so both the main generation and the single-segment fix use the same voice consistently.

### About voice cloning and this pipeline

I also have a separate `.bat` that launches Pocket TTS's own HTML GUI (`pocket-tts serve`), which is the easiest way to clone a voice interactively. It's **not included in this repo**, because it's just a thin launcher for the official GUI — nothing custom of mine to add. More importantly: once you generate audio *through* that GUI, you're outside this pipeline entirely, so none of the hallucination detection/rescue/segment-regeneration tooling here applies to it. Use the official GUI to clone and test a voice, but come back to `pocket_tts_expressive.py` (with `--voice` pointed at your cloned voice or its exported `.safetensors` file) for actual long-form generation with QA.

## Markup syntax

Three inline tags can be placed anywhere in the text, and apply to everything after them until the next tag (or end of paragraph):

| Tag | Meaning | Example |
|---|---|---|
| `$<value>` | Set base temperature from this point on | `$0.5` |
| `£<value>` | Set speed from this point on | `£0.9` |
| `%<seconds>` | Insert a silent pause of the given duration | `%1.0` |

Example:

```
Questa frase è letta normale. $0.4 £0.85 Questa invece è più lenta e stabile. %1.2 Dopo una pausa di un secondo, si riparte.
```

Each paragraph (separated by a blank line, or a line break if your file has one sentence per line) resets to the base temperature/speed passed via `--base-temperature` / `--base-speed`.

`...` in the text is automatically converted into an explicit, fixed-duration pause instead of being read aloud with an unpredictable length (configurable with `--ellipsis-pause-sec`, default 0.45s).

## Basic usage

```bash
python pocket_tts_expressive.py input.txt output.wav \
    --language italian_24l --voice giovanni \
    --base-temperature 0.7 --base-speed 1.0 \
    --debug
```

Or just run `genera_espressivo_debug.bat` and follow the prompts (it hardcodes the flags above so it's always clear what a given run used). This `.bat` is a plain command-line launcher — it has nothing to do with Pocket TTS's own web GUI (`pocket-tts serve`) and doesn't need it running. Two ways to use it:
- **Drag and drop** a `.txt` file directly onto the `.bat` file's icon in Windows Explorer — it will pick it up as the input automatically.
- Or just **double-click** the `.bat` and type the filename when prompted.

Either way it'll then ask for the output filename (or accept the default) and proceed.

`--debug` writes every generated segment as an individual `.wav` plus a `manifest.csv` in a `<output>_debug/` folder — this is what the segment-regeneration tool reads from.

### Fixing a single bad segment

After a run, open `manifest.csv` and look at the `flag` column for anything other than `ok` (e.g. `sospetto_allucinazione`, `sospetto_troncamento`, `sospetto_volume_anomalo`). Note its `indice`, then either:

```bash
python rigenera_segmento.py output_debug 12 output.wav 0.3 0.9
```

or run `rigenera_segmento_espressivo_v2.bat`, which asks for the segment index, temperature and speed interactively. Either way, it regenerates only that segment and rebuilds the complete WAV from the (mostly unchanged) manifest — no need to reprocess the whole file.

## How the anti-hallucination pipeline works

Longer texts fed to a TTS model in one shot tend to drift, skip words, or trail off into noise. This pipeline mitigates that with several layers, all automatic:

- **Sentence-aware chunking**: text is split into sentences, and any sentence over `--max-words` (default 18) is further split at clause boundaries (commas, semicolons) or, failing that, near a natural conjunction — never at an arbitrary word-count cut if a better boundary exists nearby.
- **Contextual lead-in**: each segment is generated as a fresh utterance, and TTS models tend to hallucinate on that "cold start". By default (`--lead-in-mode context`) each segment is prefixed with the last few words of the *actual preceding text* (not a generic filler), then trimmed off after generation — giving the model something real to build momentum from.
- **Duration-ratio flagging**: segments whose seconds-per-word ratio is far from the file's median get flagged as likely truncated or hallucinated.
- **Optional ASR cross-check** (`--asr-verify`): transcribes each segment locally with `faster-whisper` and compares it against the source text.
- **Automatic rescue**: flagged segments are retried with adjusted temperature, and if still bad, split into two shorter generations and stitched back together.
- **Volume normalization**: segments that are consistently too quiet/loud (but otherwise fine) get their gain corrected in post rather than being regenerated forever.

Everything above is tunable via CLI flags — run `python pocket_tts_expressive.py --help` for the full list.

**A practical note on `--workers`**: each worker process loads its own copy of the model, so more workers means more RAM used. If the program crashes (Windows sometimes reports this as an "access violation"), the most common fix is simply lowering `--workers` in the `.bat` file (default is 6) — try 4 or 2 depending on your available RAM, especially on machines with 8GB or less.

## Credits

- Core engine and model: [Kyutai](https://kyutai.org) — [Pocket TTS](https://github.com/kyutai-labs/pocket-tts) (MIT License).
- Markup system, QA/rescue pipeline, and segment-regeneration tooling: built and extensively tested by [your name/handle here], with coding assistance from Claude (Anthropic) and DeepSeek. I'm not a developer by background — every line here was tested in practice until it reliably worked, not written from theoretical knowledge.

## License

MIT, same as the upstream project — see [LICENSE](LICENSE).
