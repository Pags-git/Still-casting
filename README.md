# Still Casting

## Casting development branch

This separate source checkout adds **☰ → Cast…** for Google Cast-enabled TVs, including Sony Google TV, and DLNA/UPnP AVTransport renderers such as compatible Samsung TVs. Both appear in one list and use the same **Start casting**, Play, Pause and Stop casting buttons. Install `requirements.txt`, open a local video, choose **Cast…**, select the TV and press **Start casting**. Use **Refresh** to scan again. Closing the panel leaves casting running, while closing the player stops it. If a TV advertises both protocols at the same address, Google Cast is preferred.

The computer and TV must share a reachable local network with Google Cast or media sharing enabled. Discovery uses multicast DNS for Google Cast and SSDP for DLNA; the TV must also be able to reach the temporary HTTP port on the computer. Guest-network isolation or firewall rules can prevent this. Samsung may ask you to allow the computer to share media. Only the selected video is served, using an unguessable URL, while the cast session is active. Keep the computer awake.

This is direct file casting, without transcoding. Format support depends on the TV; start with an H.264/AAC MP4. The local subtitle overlay and selected audio track are not transferred to the TV. The cast begins from the current local position; local playback pauses when the receiver reports playback ready. The main playback controls continue to control the local player; use the casting panel or TV remote for the TV. TV playback positions are not written back to local resume history.

The casting player has a separate single-instance identity and settings file from the original Still. Sony Google Cast playback was confirmed by Pags. DLNA descriptions, control requests and shared-button routing have automated tests; physical Samsung playback still needs testing. DLNA seeking may be unsupported on some TVs, in which case playback starts from the beginning and the panel reports this. The Linux casting AppImage includes both Google Cast and DLNA support; Samsung playback remains unverified.

<img src="packaging/still-128.png" width="80" alt="Still icon">

A minimalist, frameless video player built with Python, Qt6 and PySide6.

**An App by Pags · Built with OpenAI Codex**

![Still welcome screen](docs/screenshot.png)

## Download — v0.1.5-casting

This is an experimental casting edition of [Still](https://github.com/Pags-git/Still), maintained separately so the original player remains independent.

Download the Linux x86_64 [Still-Casting-x86_64.AppImage](https://github.com/Pags-git/Still-casting/releases/download/v0.1.5-casting/Still-Casting-x86_64.AppImage) and its checksum from the [casting release](https://github.com/Pags-git/Still-casting/releases/tag/v0.1.5-casting).

```sh
sha256sum -c Still-Casting-x86_64.AppImage.sha256
chmod +x Still-Casting-x86_64.AppImage
./Still-Casting-x86_64.AppImage
```

If FUSE is unavailable, add `--appimage-extract-and-run`. Windows casting builds are not provided in this repository. The Windows installer in the original Still repository does not include casting.

## Features

- Video-first window with no native title bar or menu bar.
- Floating controls appear on mouse-over and fade when idle. Leaving the window waits one second, then fades them over 800 ms.
- Click anywhere on the seek bar to jump, or drag to preview frames.
- Double-click to toggle fullscreen and restore the previous window size and position.
- Resize from all four corners and all four edges; drag the filename area to move the window.
- One instance per user/display session: opening another video reuses the existing window.
- A compact hamburger menu for subtitles and keyboard shortcuts.
- Embedded text subtitles and external SRT/VTT files, rendered as outlined text without a background box.
- Automatic selection of supported text tracks explicitly marked forced; normal/default tracks stay off.
- Outlined volume, mute, subtitle and seek feedback: a one-second hold followed by a 700 ms fade.
- Focus-on-hover requests keyboard focus without requiring a click. Desktop focus policies may limit this.
- Local playback; no account, uploads or telemetry.

## Run from source

Use Python 3.10 or newer. The pinned Linux AppImage build uses Python 3.12.

```sh
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python player.py
```

Open a local file directly:

```sh
python player.py "/path/to/video.mkv"
```

On Windows, use `py -m venv .venv`, activate with `.venv\Scripts\activate`, and run with `python`. The casting edition has not been packaged or validated on Windows.

Opening Still without a file shows its welcome screen. Opening a file directly skips that screen and starts with controls hidden until pointer movement or re-entry. Close previous versions completely before trying an updated build.

## Subtitles

Use **☰ → Subtitles** to select an embedded language, turn subtitles **Off**, or **Load subtitles…** from an `.srt` or `.vtt` file. Subtitle files can also be dropped onto an open video.

External subtitles remain loaded when switched off. Opening another video clears that file. Embedded entries show their language, with track numbers for duplicate languages and a `(forced)` marker when applicable.

Forced-track detection uses bundled PyAV/FFmpeg metadata in an isolated background process. If multiple supported forced tracks exist, the active audio language is preferred; otherwise the first is used. Manual selection or Off takes priority for that video. Missing flags, an unsuccessful probe, or uncertain track mapping leaves normal subtitles off.

Text formatting is simplified to plain text. Image-based PGS/VobSub subtitles and advanced ASS/SSA styling are not supported.

## Keyboard shortcuts

Available inside the app through **☰ → Keyboard shortcuts**, or **F1**.

| Key | Action |
| --- | --- |
| Space | Play / pause |
| O | Open video |
| F | Toggle fullscreen |
| Esc | Exit fullscreen; close the shortcut panel when active |
| M | Mute / unmute |
| Left / Right | Back / forward 10 seconds |
| Up / Down | Volume up / down 5% |
| V | Toggle selected subtitles; select the first embedded track if none selected |
| Ctrl+Shift+S | Load subtitle file |
| F1 | Show keyboard shortcuts |
| Ctrl+Q | Quit |

## Build an AppImage

On Linux x86_64, inside a virtual environment with Python 3.12 or newer:

```sh
pip install -r requirements-build.txt
python build_appimage.py
```

Output: `dist/Still-Casting-x86_64.AppImage`, with a SHA-256 checksum alongside it. Build intermediates are under `work/appimage/` and are excluded from Git.

The image bundles Python, PySide6, Qt, multimedia libraries and PyAV. The host still provides graphics drivers, desktop/display and audio services, and baseline Linux libraries. The current build was made on Zorin OS 18.1 / Ubuntu 24.04 (glibc 2.39); compatibility with older Linux systems is not guaranteed.

```sh
chmod +x dist/Still-Casting-x86_64.AppImage
./dist/Still-Casting-x86_64.AppImage
```

If FUSE is unavailable:

```sh
./dist/Still-Casting-x86_64.AppImage --appimage-extract-and-run
```

The builder downloads the official `appimagetool` and checks its checksum. Its continuous release URL can change; a changed download is rejected until the checksum is reviewed. Large binaries are not committed to this source repository.

## Format support and validation

Playback is provided by Qt Multimedia/FFmpeg. MKV and MP4 playback, H.264/H.265 content, embedded text subtitles and external SRT/VTT have been exercised during development. Other containers/codecs depend on the bundled backend; a filename extension alone does not guarantee playback. DRM-protected content is not supported.

Development checks covered subtitle parsing, forced flags, seek previews, OSD/notification timing, fullscreen restoration, resize geometry, single-instance handoff and crash recovery, plus standalone Linux startup. The casting edition adds automated checks for DLNA descriptions and SOAP commands, shared casting controls and discovery deduplication. Sony Google Cast playback was confirmed by Pags. Samsung DLNA playback and Windows casting remain unverified.

## Project files

- `player.py`: playback, window, OSD, menus and shortcuts.
- `subtitles.py`: SRT/VTT parsing and text normalization.
- `forced_subtitles.py`: container subtitle flag inspection.
- `single_instance.py`: local socket handoff and crash-safe locking.
- `build_appimage.py`: Linux packaging.
- `packaging/`: launcher metadata and icon assets.
