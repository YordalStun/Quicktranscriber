# QuickTranscriber

**Private meeting transcription and AI meeting notes that run 100% on your own computer.**

Record a meeting (or drop in any audio/video file) and QuickTranscriber will:

1. **Transcribe** it word-for-word with OpenAI Whisper - accurate even for long, messy meetings.
2. **Work out who spoke when** and **recognise people it has met before** by their voice.
3. **Write meeting notes** with a local AI model: summary, key points, decisions, action items with owners, open questions and chapters.

Nothing is uploaded anywhere. There are no accounts, subscriptions or cloud services. The internet is only used when *you* choose to download a model.

---

## Download & start (Windows)

1. Download `QuickTranscriber-Windows.zip` from the [latest release](https://github.com/YordalStun/Quicktranscriber/releases/latest).
2. Extract it anywhere you like - right-click the zip → **Extract All** (e.g. into `Documents`). Don't run it from inside the zip.
3. Open the extracted folder and double-click **`QuickTranscriber.exe`**.
   *If Windows SmartScreen says "Windows protected your PC", click **More info → Run anyway** (the app isn't code-signed).*
4. The setup guide asks for your meeting language and a quality level, then downloads the models once.

A QuickTranscriber icon appears in the system tray (bottom-right): right-click it to **open the app** or **quit**.

**NVIDIA graphics card?** Open **AI models → Enable GPU** (a one-time 1.3 GB download). Transcription becomes 5-20× faster. Everything also works on low-end laptops without a GPU - it just takes longer.

### Other ways to run it

| System | How |
| --- | --- |
| Windows (from the source code) | Double-click `QuickTranscriber.exe` or `Start QuickTranscriber.bat`. The first start sets everything up inside the folder. |
| macOS | Double-click `Start QuickTranscriber.command` (or run it from Terminal). |
| Linux | `./start-linux.sh` |

The first start downloads a private copy of Python and the libraries (about 400 MB) **into the app folder**. Nothing is installed anywhere else on your computer - no registry entries, no system Python. **To uninstall, delete the folder.**

---

## Everything lives in one folder

```
QuickTranscriber/
├── QuickTranscriber.exe      start here (Windows)
├── data/                     your meetings, transcripts, notes, voice library (created on first use)
├── models/                   downloaded AI models
├── runtime/                  private Python, libraries and the AI engine
└── quicktranscriber/         the app itself
```

Back up `data/` to keep your meetings. Copy the whole folder to move the app to another computer (use the same operating system).

---

## Features

### Recording
- Record from any microphone, or **microphone + computer audio** for Teams / Zoom / Meet calls (tick *Share audio* when asked).
- Audio is saved to disk every few seconds, so a crash or closed window never loses a long meeting - unfinished recordings are offered for rescue.
- Live level meter and waveform, pause/resume, **bookmarks** (press `B`) that appear in the transcript, and the screen is kept awake while recording.

### Transcription
- Any audio or video format (MP3, M4A, WAV, OGG, FLAC, WEBM, MP4, MOV, MKV…), any length - multi-hour meetings are streamed, not loaded into memory.
- Choose the model per meeting: from *Whisper Base* (fast) to *Whisper Large v3* (maximum accuracy). The app shows how long each will take **on your computer** and learns your real speed over time.
- Custom vocabulary (names, places, jargon) for better spelling. Names from your speaker library are added automatically.
- 99 languages. Choose the meeting language for best results - small models sometimes mistake accents for another language.

### Speakers that learn
- Voices are separated automatically (pyannote segmentation + NVIDIA TitaNet voiceprints). On the AMI meeting benchmark the default model finds the right number of speakers with ~19% diarization error - on par with pyannote 3.1.
- **Name a speaker once and they're recognised in future meetings.** Suggestions below the confidence threshold ask *"Is this Mark?"* - Yes / No.
- Fix mistakes easily: rename, merge two speakers, move a single line to another person, or change the number of people - **re-grouping is instant**.
- **The Speakers page is your voice library**: listen to every learned sample, remove bad ones, clean up clips that "don't sound like the others", record a 20-second enrolment sample, merge people, or forget everything learned from a specific meeting.

### AI meeting notes
- Templates: general meeting, **committee / formal minutes**, stand-up, project update, 1:1, client call, interview, lecture, brainstorm - plus your own instructions.
- Long meetings are handled with map-reduce: the transcript is read in parts that fit the model, then merged, so 3-hour meetings work even on small models.
- Output is constrained to structured JSON, so notes are always well-formed - if a very long answer runs out of room, everything complete is kept. Timestamps are clickable and jump to that moment in the audio.
- Tick off action items; the **Action items** page collects open tasks from every meeting.
- **Ask your meeting**: *"What did we decide about the budget?"* - answers cite clickable timestamps.

### AI models (all local)

| Level | Speech model | Notes model | Good for |
| --- | --- | --- | --- |
| Fast | Whisper Small | Qwen 3.5 2B | Older/low-power laptops |
| Balanced | Whisper Large v3 Turbo | Qwen 3.5 4B | Most computers (recommended) |
| Best | Whisper Large v3 | Qwen 3.5 9B / Gemma 4 12B | NVIDIA GPU or 16 GB+ RAM |

More notes models are available (Llama 3.2, Gemma 4 E4B/26B, gpt-oss 20B, Qwen 3.6 35B), plus **any GGUF model from Hugging Face** via a link, or `.gguf` files dropped into `models/llm/`. If you already use **Ollama** or **LM Studio**, you can point QuickTranscriber at them in Settings.

The AI engine is [llama.cpp](https://github.com/ggml-org/llama.cpp). The right build is downloaded automatically: CUDA for NVIDIA cards, Metal on Apple Silicon, CPU otherwise.

**How long does it take?** Measured on a 4-core computer *without* a graphics card, for a 1 h 41 min meeting with the Balanced models: transcription 34 min, speakers 4 min, AI notes about an hour. You can read, search and play the transcript while the notes are written. An NVIDIA graphics card makes every step much faster; on a slow laptop the Fast models keep waiting times down.

### Export
TXT, Word (.docx), PDF (print-ready page), Markdown, HTML, subtitles (SRT / WebVTT), JSON, or a ZIP with everything including the audio. Choose whether to include notes, transcript, timestamps and speaker names. **Copy notes** puts them on the clipboard for email or chat.

### Nice touches
- Karaoke-style word highlighting while playing; click any word to jump there.
- Waveform coloured by speaker with chapter markers; playback speed 0.75-2×.
- Search every word of every meeting (`Ctrl+K`), with matches highlighted in the transcript.
- Talk-time chart, words per minute and longest monologue per meeting.
- Keyboard: `Space` play/pause, `J`/`L` ±15 s, `←`/`→` ±5 s, `[`/`]` speed, `Ctrl+F` search the transcript.
- Desktop notification when a long meeting finishes processing. Dark and light themes.

---

## Privacy & security

- Audio, transcripts, notes and voiceprints never leave your computer.
- The app's web interface listens only on `127.0.0.1` (not your network). It checks the `Host` header and requires a private header on every change, so other websites cannot talk to it.
- Downloads come only from the model hosts you choose (Hugging Face, GitHub releases for llama.cpp / uv / speaker models, PyPI for libraries).

## Troubleshooting

- **"Windows protected your PC"** - click *More info → Run anyway*. The zip is built automatically by GitHub Actions from this repository's source code.
- **"Can't find its files next to QuickTranscriber.exe"** - the zip wasn't extracted. Right-click the zip → *Extract All*, then start the exe from the extracted folder.
- **Blocked by Smart App Control** (some new Windows 11 PCs) - Smart App Control only allows code-signed apps, and QuickTranscriber isn't signed. It runs once Smart App Control is turned off (Windows Security → App & browser control).
- **Wrong language / gibberish transcript** - set the meeting language instead of *detect automatically*, or use a larger model.
- **Speakers mixed up** - open the *Speakers* tab of the meeting and set the number of people; name the speakers so they're learned.
- **GPU not used** - AI models page → *Enable GPU*. If the GPU fails, QuickTranscriber automatically falls back to the processor and tells you why.
- **Logs** are in `data/logs/` (`app.log`, `llama-server.log`, `launcher.log`).

---

## For developers

- Backend: Python 3.12, FastAPI, faster-whisper (CTranslate2), sherpa-onnx + onnxruntime, SQLite. Heavy work runs in child processes so a crash or GPU problem can never take down the app.
- Front end: dependency-free ES modules in `quicktranscriber/static` - no build step.
- Launcher: `launcher/` (Go). Build with `GOOS=windows go build -ldflags "-H windowsgui" -o QuickTranscriber.exe` after `go-winres make`.
- Run from source: `python -m quicktranscriber --verbose` (add `--no-browser` to skip opening a window).
- CI: `.github/workflows/windows.yml` builds the portable Windows package and runs `ci/smoke_test.py` end to end on Windows.

### Credits
OpenAI Whisper · SYSTRAN faster-whisper · pyannote segmentation 3.0 · NVIDIA NeMo TitaNet · k2-fsa sherpa-onnx · ggml-org llama.cpp · Qwen, Gemma, Llama and gpt-oss model authors · Astral uv · icons adapted from Lucide (ISC).
