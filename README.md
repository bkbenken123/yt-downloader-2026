Project was made by bkbenken123

# YouTube Downloader

A lightweight desktop YouTube downloader built with **Rust**, **yt-dlp**, and **FFmpeg**. Download videos, extract audio, or save thumbnails through a simple graphical interface.

---

## Features

* Download videos using **yt-dlp**
* Download audio only
* Download video thumbnails
* Support for multiple output formats and codecs
* Automatically detects required binaries locally or from your system **PATH**

---

## Requirements

* Rust 1.99 or newer
* yt-dlp
* FFmpeg
* FFprobe

---

## Installation

Clone the repository

Build and run the application:

```bash
cargo run --release
```

The application keeps using `binaries/ffmpeg`, `binaries/ffmpeg-build`, `binaries/yt-dlp`, `binaries/yt-dlp_linux`, `config.json`, and the system `PATH` for tool discovery. The bundled yt-dlp source checkout is supported when a `python3` or `python` executable is available; a standalone yt-dlp executable is preferred.

---

## License

Licensed under the **MPL License**.

---

## Disclaimer

This project is intended for downloading content that you own or are authorized to access. You are responsible for complying with applicable copyright laws and the terms of service of the websites you use.
