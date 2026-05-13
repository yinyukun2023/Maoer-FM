# Maoer-FM

Maoer-FM is a Windows desktop client for browsing and playing Maoer FM content.
It is built with Python, wxPython, requests, and Windows audio/WebView
integration.

## Features

- Browse and play Maoer FM audio content.
- Account login, favorites, subscriptions, check-in, and purchase flows.
- Hidden browser playback support for web-based media playback.
- Playback speed, volume, danmaku/subtitle, and hotkey support.
- Windows build and updater tooling.

## Requirements

- Windows 10 or later.
- Python 3.10 or later.
- Microsoft Edge WebView2 Runtime.

Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

## Run

```powershell
python app.py
```

## Build

Run the build UI:

```powershell
.\build_auto.bat
```

or run the build script directly:

```powershell
python build_exe.py --ui
```

## Account Data

Login cookies are stored under the user's application data directory and should
not be committed. The legacy local files `cookey` and `cookies.txt` are ignored
by Git.

## License

This project is licensed under the MIT License. See [LICENSE](LICENSE).
