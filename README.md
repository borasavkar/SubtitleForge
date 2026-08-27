# SubtitleForge

Welcome to **SubtitleForge**, an AI-powered desktop application designed to generate, align, and translate subtitles effortlessly. Running entirely on your CPU, it utilizes the powerful WhisperX model to deliver high-quality transcription and precise word-level alignments.

## Features

- **Offline Support**: Run the application entirely offline. Just place the required models in a `models` folder next to the executable.
- **Accurate Transcription**: Built on WhisperX for robust, time-accurate speech recognition.
- **Smart Hallucination Protection**: Custom logic to detect and prevent Whisper's repetitive loop hallucinations on long segments of silence or music.
- **Multi-Window Language Detection**: Scans multiple parts of a video to correctly identify the language, bypassing Whisper's initial 30-second limitation.
- **Reliable Batched Translation**: Subtitle translation powered by Google Translate over an endpoint that preserves line boundaries, so a 40-line batch costs one request instead of forty. Misaligned batches are bisected rather than abandoned, and any lines lost to a rate limit are retried in dedicated repair rounds — no silently untranslated subtitles.
- **Uncensored Mode**: Profanity and slang are transcribed and translated verbatim instead of being softened or masked. Can be toggled off in the UI.
- **Fluent & Accessible UI**: A dark-themed, modern graphical interface built with Python's Tkinter.

## Installation & Usage

You can download the pre-compiled, ready-to-use version from the [Releases](https://github.com/borasavkar/SubtitleForge/releases) page. 

1. Download `SubtitleForge_Release.zip` from the latest release.
2. Extract the archive to your desired location.
3. Run the executable inside the extracted folder to start the application. 

### Running from Source

If you prefer to run it from source:

1. Clone this repository.
2. Install the required dependencies: `pip install -r requirements.txt`
3. Run `python run.py`

## Building the Executable

This project uses PyInstaller. You can build the standalone executable yourself using the provided `run.spec` file:

```bash
pyinstaller run.spec
```

### Versioning

The version number lives in a single place: `surum.py`. Bump `SURUM` there and everything else follows.

Every build stamps itself so you can always tell whether the executable you are holding is current:

- **Window title** — `SubtitleForge v1.1.0`
- **First line of the log panel** — `SubtitleForge v1.1.0 · 2026-08-27 20:47 derlemesi · b8472c8` (build timestamp and the git commit it was built from). Running from source shows `kaynaktan çalışıyor` instead.
- **Windows file properties** — right-click `run.exe` → Properties → Details shows File version and Product version.
- **Crash reports** — `HATA_RAPORU.txt` starts with the version line, so a shared report identifies its build.

The build timestamp and commit are written into a generated `_derleme_bilgisi.py` by `run.spec` at build time; it is git-ignored and never edited by hand.

## Contributing

Feel free to open issues or submit pull requests. Any contributions that improve the accuracy, speed, or user experience are highly appreciated.
