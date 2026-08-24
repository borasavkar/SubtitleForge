# SubtitleForge

Welcome to **SubtitleForge**, an AI-powered desktop application designed to generate, align, and translate subtitles effortlessly. Running entirely on your CPU, it utilizes the powerful WhisperX model to deliver high-quality transcription and precise word-level alignments.

## Features

- **Offline Support**: Run the application entirely offline. Just place the required models in a `models` folder next to the executable.
- **Accurate Transcription**: Built on WhisperX for robust, time-accurate speech recognition.
- **Smart Hallucination Protection**: Custom logic to detect and prevent Whisper's repetitive loop hallucinations on long segments of silence or music.
- **Multi-Window Language Detection**: Scans multiple parts of a video to correctly identify the language, bypassing Whisper's initial 30-second limitation.
- **Built-in Translation**: Seamless subtitle translation powered by Google Translate, optimized with batched processing to prevent rate limits and speed up translation.
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

## Contributing

Feel free to open issues or submit pull requests. Any contributions that improve the accuracy, speed, or user experience are highly appreciated.
