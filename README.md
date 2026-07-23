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

Or just run `genera_espressivo_debug.bat` and follow the prompts (it hardcodes the flags above so it's always clear what a given run used).

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

## Credits

- Core engine and model: [Kyutai](https://kyutai.org) — [Pocket TTS](https://github.com/kyutai-labs/pocket-tts) (MIT License).
- Markup system, QA/rescue pipeline, and segment-regeneration tooling: built and extensively tested by music-and-arts, with coding assistance from Claude (Anthropic) and DeepSeek. I'm not a developer by background — every line here was tested in practice until it reliably worked, not written from theoretical knowledge.

## License

MIT, same as the upstream project — see [LICENSE](LICENSE).
