use eframe::egui;
use regex::Regex;
use rfd::FileDialog;
use serde::{Deserialize, Serialize};
use serde_json::Value;
use std::{
    collections::HashMap,
    env, fs,
    io::{BufRead, BufReader},
    path::{Path, PathBuf},
    process::{Command, Stdio},
    sync::mpsc::{self, Receiver, Sender},
    thread,
    time::Duration,
};

const VERSION: &str = "2026.10.30.1";
const VIDEO_CONTAINERS: &[&str] = &["mp4", "mkv", "webm", "mov"];
const AUDIO_CONTAINERS: &[&str] = &["mp3", "m4a", "wav", "flac", "opus"];
const THUMBNAIL_CONTAINERS: &[&str] = &["jpg", "png", "webp"];
const VIDEO_CODECS: &[&str] = &[
    "copy",
    "h264",
    "h265",
    "vp9",
    "av1",
    "prores_422",
    "dnxhr_sq",
    "dnxhr_hq",
];
const AUDIO_CODECS: &[&str] = &["aac", "opus", "flac", "lpcm", "mpeg-1", "copy"];
const BACKENDS: &[&str] = &["CPU", "AMD", "INTEL", "NVIDIA"];
const COVER_AUDIO: &[&str] = &["mp3", "m4a", "flac", "opus"];

#[derive(Clone, Serialize, Deserialize)]
struct Settings {
    encoder_backend: String,
    default_download_dir: String,
    download_playlist: bool,
    use_ytdlp_audio_conversion: bool,
    embed_metadata: bool,
}

impl Default for Settings {
    fn default() -> Self {
        Self {
            encoder_backend: "CPU".into(),
            default_download_dir: default_download_dir(),
            download_playlist: true,
            use_ytdlp_audio_conversion: true,
            embed_metadata: false,
        }
    }
}

#[derive(Clone)]
struct Options {
    mode: String,
    container: String,
    video_codec: String,
    audio_codec: String,
    outdir: PathBuf,
    settings: Settings,
}

enum Event {
    Log(String),
    Error(String),
    Done(PathBuf),
    Startup(bool, bool),
    Update(bool, bool, bool, String),
}

fn app_dir() -> PathBuf {
    env::current_exe()
        .ok()
        .and_then(|p| p.parent().map(Path::to_path_buf))
        .unwrap_or_else(|| PathBuf::from("."))
}
fn binaries_dir() -> PathBuf {
    app_dir().join("binaries")
}
fn ffmpeg_source() -> PathBuf {
    binaries_dir().join("ffmpeg")
}
fn ffmpeg_output() -> PathBuf {
    binaries_dir().join("ffmpeg-build").join("bin")
}
fn config_path() -> PathBuf {
    app_dir().join("config.json")
}
fn default_download_dir() -> String {
    env::var_os("HOME")
        .map(|p| PathBuf::from(p).join("Downloads"))
        .or_else(|| env::var_os("USERPROFILE").map(|p| PathBuf::from(p).join("Downloads")))
        .unwrap_or_else(|| PathBuf::from("Downloads"))
        .to_string_lossy()
        .into_owned()
}

fn load_settings() -> (Settings, Option<String>) {
    let path = config_path();
    if !path.is_file() {
        return (Settings::default(), None);
    }
    match fs::read_to_string(path)
        .ok()
        .and_then(|s| serde_json::from_str::<Value>(&s).ok())
    {
        Some(v) => {
            let mut s = Settings::default();
            if let Some(x) = v.get("encoder_backend").and_then(Value::as_str) {
                if BACKENDS.contains(&x.to_ascii_uppercase().as_str()) {
                    s.encoder_backend = x.to_ascii_uppercase();
                }
            }
            if let Some(x) = v.get("default_download_dir").and_then(Value::as_str) {
                if !x.trim().is_empty() {
                    s.default_download_dir = expand_path(x);
                }
            }
            if let Some(x) = v.get("download_playlist").and_then(Value::as_bool) {
                s.download_playlist = x;
            }
            if let Some(x) = v.get("use_ytdlp_audio_conversion").and_then(Value::as_bool) {
                s.use_ytdlp_audio_conversion = x;
            }
            if let Some(x) = v.get("embed_metadata").and_then(Value::as_bool) {
                s.embed_metadata = x;
            }
            (s, None)
        }
        None => (
            Settings::default(),
            Some("Could not read config.json; defaults were loaded.".into()),
        ),
    }
}

fn save_settings(s: &Settings) -> Result<(), String> {
    let default = Settings::default();
    let mut object = serde_json::Map::new();
    if s.encoder_backend != default.encoder_backend {
        object.insert(
            "encoder_backend".into(),
            Value::String(s.encoder_backend.clone()),
        );
    }
    if s.default_download_dir != default.default_download_dir {
        object.insert(
            "default_download_dir".into(),
            Value::String(s.default_download_dir.clone()),
        );
    }
    if s.download_playlist != default.download_playlist {
        object.insert("download_playlist".into(), Value::Bool(s.download_playlist));
    }
    if s.use_ytdlp_audio_conversion != default.use_ytdlp_audio_conversion {
        object.insert(
            "use_ytdlp_audio_conversion".into(),
            Value::Bool(s.use_ytdlp_audio_conversion),
        );
    }
    if s.embed_metadata != default.embed_metadata {
        object.insert("embed_metadata".into(), Value::Bool(s.embed_metadata));
    }
    let path = config_path();
    if object.is_empty() {
        if path.is_file() {
            fs::remove_file(path).map_err(|e| e.to_string())?;
        }
        return Ok(());
    }
    let tmp = path.with_extension("json.tmp");
    fs::write(
        &tmp,
        serde_json::to_string_pretty(&object).map_err(|e| e.to_string())? + "\n",
    )
    .map_err(|e| e.to_string())?;
    fs::rename(tmp, path).map_err(|e| e.to_string())
}

fn expand_path(s: &str) -> String {
    if let Some(rest) = s.strip_prefix("~/") {
        if let Some(home) = env::var_os("HOME").or_else(|| env::var_os("USERPROFILE")) {
            return PathBuf::from(home)
                .join(rest)
                .to_string_lossy()
                .into_owned();
        }
    }
    s.to_string()
}
fn exe_name(name: &str) -> String {
    if cfg!(windows) {
        format!("{name}.exe")
    } else {
        name.into()
    }
}
fn find_executable(name: &str) -> Option<PathBuf> {
    let candidates = [
        binaries_dir().join(exe_name(name)),
        app_dir().join(exe_name(name)),
    ];
    candidates.into_iter().find(|p| p.is_file()).or_else(|| {
        env::var_os("PATH").and_then(|path| {
            env::split_paths(&path)
                .map(|p| p.join(exe_name(name)))
                .find(|p| p.is_file())
        })
    })
}
fn ffmpeg(name: &str) -> Option<PathBuf> {
    let local = ffmpeg_output().join(exe_name(name));
    if local.is_file() {
        Some(local)
    } else {
        find_executable(name)
    }
}
fn ytdlp_command() -> Option<Vec<String>> {
    let linux = binaries_dir().join("yt-dlp_linux");
    if linux.is_file() {
        return Some(vec![linux.to_string_lossy().into_owned()]);
    }
    if let Some(path) = ["yt-dlp", "yt_dlp"].into_iter().find_map(find_executable) {
        return Some(vec![path.to_string_lossy().into_owned()]);
    }
    let source = binaries_dir()
        .join("yt-dlp")
        .join("yt_dlp")
        .join("__main__.py");
    if source.is_file() {
        for python in ["python3", "python"] {
            if let Some(path) = find_executable(python) {
                return Some(vec![
                    path.to_string_lossy().into_owned(),
                    source.to_string_lossy().into_owned(),
                ]);
            }
        }
    }
    None
}
fn command_string(parts: &[String]) -> String {
    if cfg!(windows) {
        parts
            .iter()
            .map(|p| format!("\"{}\"", p.replace('"', "\\\"")))
            .collect::<Vec<_>>()
            .join(" ")
    } else {
        parts
            .iter()
            .map(|p| {
                if p.contains([' ', '\'', '"']) {
                    format!("'{}'", p.replace('\'', "'\\''"))
                } else {
                    p.clone()
                }
            })
            .collect::<Vec<_>>()
            .join(" ")
    }
}
fn stream_command(mut cmd: Command, tx: &Sender<Event>, prefix: &str) -> i32 {
    let shown = format!("{:?}", cmd);
    let _ = tx.send(Event::Log(format!("Running: {shown}")));
    let spawn = cmd
        .stdout(Stdio::piped())
        .stderr(Stdio::piped())
        .stdin(Stdio::null())
        .spawn();
    let mut child = match spawn {
        Ok(c) => c,
        Err(e) => {
            let _ = tx.send(Event::Error(format!("Could not start command: {e}")));
            return -1;
        }
    };
    let stdout = child.stdout.take().map(|out| {
        let tx = tx.clone();
        let prefix = prefix.to_string();
        thread::spawn(move || {
            for line in BufReader::new(out).lines().flatten() {
                if !line.trim().is_empty() {
                    let _ = tx.send(Event::Log(format!("{prefix}{line}")));
                }
            }
        })
    });
    let stderr = child.stderr.take().map(|err| {
        let tx = tx.clone();
        let prefix = prefix.to_string();
        thread::spawn(move || {
            for line in BufReader::new(err).lines().flatten() {
                if !line.trim().is_empty() {
                    let _ = tx.send(Event::Log(format!("{prefix}{line}")));
                }
            }
        })
    });
    let code = child.wait().map(|s| s.code().unwrap_or(-1)).unwrap_or(-1);
    if let Some(handle) = stdout {
        let _ = handle.join();
    }
    if let Some(handle) = stderr {
        let _ = handle.join();
    }
    code
}

fn sanitize_filename(name: &str) -> String {
    let mut out: String = name
        .chars()
        .map(|c| {
            if c.is_control() || "<>:\"/\\|?*".contains(c) {
                '_'
            } else {
                c
            }
        })
        .collect();
    while out.ends_with('.') {
        out.pop();
    }
    if out.trim().is_empty() {
        "download".into()
    } else {
        out
    }
}
fn is_music(url: &str) -> bool {
    url.split('/')
        .nth(2)
        .map(|h| h.eq_ignore_ascii_case("music.youtube.com"))
        .unwrap_or(false)
}
fn thumbnail_url(url: &str, mode: &str, embed: bool) -> String {
    if mode == "video" && embed && is_music(url) {
        url.replacen("music.youtube.com", "www.youtube.com", 1)
    } else {
        url.into()
    }
}
fn unavailable_error(s: &str) -> bool {
    let x = s.to_ascii_lowercase();
    [
        "this video is not available",
        "video unavailable",
        "this video is private",
        "private video",
        "has been removed by the uploader",
        "this video has been removed",
    ]
    .iter()
    .any(|m| x.contains(m))
}
fn snapshot(folder: &Path) -> HashMap<PathBuf, (u128, u64)> {
    let mut result = HashMap::new();
    if let Ok(entries) = fs::read_dir(folder) {
        for e in entries.flatten() {
            let p = e.path();
            let ext = p
                .extension()
                .and_then(|x| x.to_str())
                .unwrap_or("")
                .to_ascii_lowercase();
            if ![
                "mp4", "mkv", "webm", "mov", "mp3", "m4a", "wav", "flac", "opus", "jpg", "jpeg",
                "png", "webp", "m4v", "mka", "mp2", "mpg", "mpeg", "avi", "flv", "3gp", "ts",
                "m2ts", "ogg", "aac",
            ]
            .contains(&ext.as_str())
            {
                continue;
            }
            if let Ok(m) = e.metadata() {
                if let Ok(t) = m.modified() {
                    if let Ok(d) = t.duration_since(std::time::UNIX_EPOCH) {
                        result.insert(p, (d.as_nanos(), m.len()));
                    }
                }
            }
        }
    }
    result
}
fn compatible_video(container: &str, codec: &str) -> bool {
    match container {
        "mp4" => ["copy", "h264", "h265", "vp9", "av1"].contains(&codec),
        "mkv" => VIDEO_CODECS.contains(&codec),
        "webm" => ["copy", "vp9", "av1"].contains(&codec),
        "mov" => [
            "copy",
            "h264",
            "h265",
            "av1",
            "prores_422",
            "dnxhr_sq",
            "dnxhr_hq",
        ]
        .contains(&codec),
        _ => false,
    }
}
fn audio_codec_for(container: &str, codec: &str) -> Vec<String> {
    if codec == "copy" {
        return vec!["-c:a".into(), "copy".into()];
    }
    let map = [
        ("aac", "aac"),
        ("opus", "libopus"),
        ("flac", "flac"),
        ("lpcm", "pcm_s16le"),
        ("mpeg-1", "libmp3lame"),
    ];
    let name = map
        .iter()
        .find(|(k, _)| *k == codec)
        .map(|(_, v)| *v)
        .unwrap_or("copy");
    let mut a = vec!["-c:a".into(), name.into()];
    match codec {
        "aac" | "mpeg-2" => {
            a.extend(["-b:a".into(), "192k".into()]);
        }
        "opus" => {
            a.extend(["-b:a".into(), "160k".into()]);
        }
        "mpeg-1" => {
            a.extend(["-q:a".into(), "2".into()]);
        }
        _ => {}
    }
    let _ = container;
    a
}
fn compatible_audio(mode: &str, container: &str, codec: &str) -> bool {
    if mode == "video" {
        match container {
            "mp4" | "mov" => ["copy", "aac", "flac", "lpcm", "mpeg-1", "mpeg-2"].contains(&codec),
            "mkv" => AUDIO_CODECS.contains(&codec),
            "webm" => ["copy", "opus"].contains(&codec),
            _ => false,
        }
    } else {
        match container {
            "mp3" => ["copy", "mpeg-1"].contains(&codec),
            "m4a" => ["copy", "aac"].contains(&codec),
            "wav" => ["copy", "lpcm"].contains(&codec),
            "flac" => ["copy", "flac"].contains(&codec),
            "opus" => ["copy", "opus"].contains(&codec),
            _ => false,
        }
    }
}
fn video_codec_args(codec: &str) -> Vec<String> {
    match codec {
        "copy" => vec!["-c:v".into(), "copy".into()],
        "h264" => vec!["-c:v".into(), "libx264".into()],
        "h265" => vec!["-c:v".into(), "libx265".into()],
        "vp9" => vec!["-c:v".into(), "libvpx-vp9".into()],
        "av1" => vec!["-c:v".into(), "libaom-av1".into()],
        "prores_422" => vec![
            "-c:v".into(),
            "prores_ks".into(),
            "-profile:v".into(),
            "3".into(),
        ],
        "dnxhr_sq" => vec![
            "-c:v".into(),
            "dnxhd".into(),
            "-profile:v".into(),
            "dnxhr_sq".into(),
        ],
        "dnxhr_hq" => vec![
            "-c:v".into(),
            "dnxhd".into(),
            "-profile:v".into(),
            "dnxhr_hq".into(),
        ],
        _ => vec![],
    }
}
fn native_audio(container: &str, s: &Settings) -> bool {
    s.use_ytdlp_audio_conversion && (container == "mp3" || (s.embed_metadata && container == "m4a"))
}

fn run_download(urls: Vec<String>, opts: Options, tx: Sender<Event>) {
    let mut last_folder = opts.outdir.clone();
    for (index, url) in urls.iter().enumerate() {
        let _ = tx.send(Event::Log(format!(
            "=== Link {}/{}, {} ===",
            index + 1,
            urls.len(),
            url
        )));
        let mut folder = opts.outdir.clone();
        if opts.settings.download_playlist {
            if let Some(y) = ytdlp_command() {
                let o = Command::new(&y[0])
                    .args(&y[1..])
                    .args([
                        url,
                        "--dump-single-json",
                        "--flat-playlist",
                        "--no-warnings",
                        "--yes-playlist",
                    ])
                    .output();
                if let Ok(o) = o {
                    if o.status.success() {
                        if let Ok(v) = serde_json::from_slice::<Value>(&o.stdout) {
                            if v.get("entries").is_some() {
                                if let Some(t) = v.get("title").and_then(Value::as_str) {
                                    folder = folder.join(sanitize_filename(t));
                                }
                            }
                        }
                    }
                }
            }
        }
        if let Err(e) = fs::create_dir_all(&folder) {
            let _ = tx.send(Event::Error(format!("Could not create output folder: {e}")));
            continue;
        }
        let outtmpl = folder.join("%(title)s.%(ext)s");
        let before = snapshot(&folder);
        let Some(ytdlp) = ytdlp_command() else {
            let _ = tx.send(Event::Error("yt-dlp was not found.".into()));
            continue;
        };
        let download_url = thumbnail_url(url, &opts.mode, opts.settings.embed_metadata);
        let mut args = vec![
            download_url,
            "-o".into(),
            outtmpl.to_string_lossy().into_owned(),
            "--no-warnings".into(),
            "--no-color".into(),
            "--newline".into(),
            "--force-overwrites".into(),
            "--no-embed-subs".into(),
            "--no-write-subs".into(),
            "--no-write-auto-subs".into(),
            if opts.settings.download_playlist {
                "--yes-playlist".into()
            } else {
                "--no-playlist".into()
            },
        ];
        if opts.settings.download_playlist {
            args.push("--ignore-errors".into());
        }
        let native_a = opts.mode == "audio" && native_audio(&opts.container, &opts.settings);
        let native_v =
            opts.mode == "video" && opts.video_codec == "copy" && opts.audio_codec == "copy";
        let ytdlp_can_embed_thumbnail = opts.settings.embed_metadata
            && (opts.mode == "video" || matches!(opts.container.as_str(), "mp3" | "m4a"));
        if opts.settings.embed_metadata && opts.mode != "thumbnail" {
            args.extend(["--embed-metadata".into(), "--embed-chapters".into()]);
            if ytdlp_can_embed_thumbnail {
                args.extend([
                    "--embed-thumbnail".into(),
                    "--convert-thumbnails".into(),
                    "jpg".into(),
                ]);
            } else if opts.mode == "audio" && COVER_AUDIO.contains(&opts.container.as_str()) {
                args.extend([
                    "--no-embed-thumbnail".into(),
                    "--write-thumbnail".into(),
                    "--convert-thumbnails".into(),
                    "jpg".into(),
                ]);
            }
        }
        if let Some(f) = ffmpeg("ffmpeg") {
            if opts.mode != "thumbnail" {
                args.extend([
                    "--ffmpeg-location".into(),
                    f.parent()
                        .unwrap_or(Path::new("."))
                        .to_string_lossy()
                        .into_owned(),
                ]);
            }
        }
        match opts.mode.as_str() {
            "thumbnail" => args.extend(["--skip-download".into(), "--write-thumbnail".into()]),
            "audio" => {
                args.extend(["-f".into(), "bestaudio/best".into()]);
                if native_a {
                    args.extend([
                        "-x".into(),
                        "--audio-format".into(),
                        opts.container.clone().into(),
                    ]);
                }
            }
            _ => {
                args.extend([
                    "-f".into(),
                    "bestvideo+bestaudio/best".into(),
                    "--merge-output-format".into(),
                    if native_v {
                        opts.container.clone()
                    } else {
                        "mkv".into()
                    },
                ]);
            }
        }
        let _ = tx.send(Event::Log(format!(
            "Starting yt-dlp: {}",
            command_string(
                &ytdlp
                    .iter()
                    .cloned()
                    .chain(args.iter().cloned())
                    .collect::<Vec<_>>()
            )
        )));
        let output = Command::new(&ytdlp[0])
            .args(&ytdlp[1..])
            .args(&args)
            .stdout(Stdio::piped())
            .stderr(Stdio::null())
            .stdin(Stdio::null())
            .spawn();
        let mut paths = Vec::new();
        let mut errors = Vec::new();
        let mut child = match output {
            Ok(c) => c,
            Err(e) => {
                let _ = tx.send(Event::Error(format!("Failed to start yt-dlp: {e}")));
                continue;
            }
        };
        if let Some(stdout) = child.stdout.take() {
            let destination = Regex::new(r"Destination:\s+(.*)").unwrap();
            let merging = Regex::new(r#"Merging formats into\s*(?:"([^"]+)"|(.+))"#).unwrap();
            let thumb = Regex::new(r"Writing video thumbnail\s+\d+\s+to:\s+(.*)").unwrap();
            for line in BufReader::new(stdout).lines().flatten() {
                let _ = tx.send(Event::Log(line.clone()));
                if line.trim_start().starts_with("ERROR:") {
                    errors.push(line.clone());
                }
                for cap in [&destination, &merging, &thumb] {
                    if let Some(m) = cap.captures(&line) {
                        let p = m
                            .get(1)
                            .or_else(|| m.get(2))
                            .map(|x| x.as_str().trim().trim_matches('"'));
                        if let Some(p) = p {
                            let path = PathBuf::from(p);
                            paths.push(if path.is_absolute() {
                                path
                            } else {
                                folder.join(path)
                            });
                        }
                    }
                }
            }
        }
        let code = child.wait().ok().and_then(|s| s.code()).unwrap_or(-1);
        let only_unavailable = !errors.is_empty()
            && opts.settings.download_playlist
            && errors.iter().all(|e| unavailable_error(e));
        if code != 0 && !only_unavailable {
            let _ = tx.send(Event::Error(format!("yt-dlp exited with code {code}")));
        }
        let after = snapshot(&folder);
        paths.extend(
            after
                .keys()
                .filter(|p| before.get(*p) != after.get(*p))
                .cloned(),
        );
        paths.sort();
        paths.dedup();
        let thumbs: Vec<PathBuf> = paths
            .iter()
            .filter(|p| {
                THUMBNAIL_CONTAINERS.contains(
                    &p.extension()
                        .and_then(|x| x.to_str())
                        .unwrap_or("")
                        .to_ascii_lowercase()
                        .as_str(),
                )
            })
            .cloned()
            .collect();
        let mut media: Vec<PathBuf> = paths
            .into_iter()
            .filter(|p| {
                !THUMBNAIL_CONTAINERS.contains(
                    &p.extension()
                        .and_then(|x| x.to_str())
                        .unwrap_or("")
                        .to_ascii_lowercase()
                        .as_str(),
                )
            })
            .collect();
        if opts.mode == "thumbnail" {
            media = thumbs.clone();
        }
        let native = native_a || native_v;
        if native {
            media.retain(|p| {
                p.extension()
                    .and_then(|x| x.to_str())
                    .unwrap_or("")
                    .eq_ignore_ascii_case(&opts.container)
            });
        }
        let mut success = 0;
        for src in media {
            let dst = src.with_extension(&opts.container);
            let ok = if native {
                if src == dst {
                    dst.is_file()
                } else {
                    fs::rename(&src, &dst).is_ok()
                }
            } else {
                convert(
                    &src,
                    &dst,
                    &opts,
                    &tx,
                    thumbs
                        .iter()
                        .find(|t| t.file_stem() == src.file_stem())
                        .cloned(),
                )
            };
            if ok {
                success += 1;
                let _ = tx.send(Event::Log(format!("Created {}", dst.display())));
            }
        }
        if opts.mode != "thumbnail" {
            for t in thumbs {
                let _ = fs::remove_file(t);
            }
        }
        if success == 0 {
            let _ = tx.send(Event::Error(format!(
                "Link {}/{}: no output files were created successfully.",
                index + 1,
                urls.len()
            )));
        }
        last_folder = folder;
    }
    let _ = tx.send(Event::Done(last_folder));
}

fn convert(
    src: &Path,
    dst: &Path,
    opts: &Options,
    tx: &Sender<Event>,
    thumb: Option<PathBuf>,
) -> bool {
    let Some(ff) = ffmpeg("ffmpeg") else {
        let _ = tx.send(Event::Error("FFmpeg was not found.".into()));
        return false;
    };
    let tmp = dst.with_extension(format!(
        "{}.temp",
        dst.extension().and_then(|x| x.to_str()).unwrap_or("out")
    ));
    let mut c = Command::new(ff);
    c.args(["-y", "-nostdin", "-i"]).arg(src);
    if let Some(t) = thumb.as_ref() {
        if (opts.mode == "audio"
            && opts.settings.embed_metadata
            && COVER_AUDIO.contains(&opts.container.as_str()))
            || (opts.mode == "video"
                && opts.settings.embed_metadata
                && ["mp4", "mov"].contains(&opts.container.as_str()))
        {
            c.args(["-i"]).arg(t);
        }
    }
    if opts.mode == "thumbnail" {
        c.args(["-frames:v", "1"]);
    } else if opts.mode == "audio" {
        c.args(["-map", "0:a:0?", "-vn", "-map_metadata", "0"])
            .args(audio_codec_for(&opts.container, &opts.audio_codec));
        if opts.settings.embed_metadata
            && COVER_AUDIO.contains(&opts.container.as_str())
            && thumb.is_some()
        {
            c.args([
                "-map",
                "0:a:0?",
                "-map",
                "1:v:0?",
                "-c:v",
                "mjpeg",
                "-disposition:v",
                "attached_pic",
            ]);
        }
    } else {
        c.args([
            "-map",
            "0:v:0?",
            "-map",
            "0:a:0?",
            "-map_metadata",
            "0",
            "-sn",
        ])
        .args(video_codec_args(&opts.video_codec))
        .args(audio_codec_for(&opts.container, &opts.audio_codec));
        if opts.settings.embed_metadata {
            c.args(["-map_chapters", "0"]);
        }
        if thumb.is_some() && ["mp4", "mov"].contains(&opts.container.as_str()) {
            c.args([
                "-map",
                "1:v:0?",
                "-c:v:1",
                "mjpeg",
                "-disposition:v:1",
                "attached_pic",
            ]);
        } else if let Some(t) = thumb.as_ref() {
            if opts.settings.embed_metadata && opts.container == "mkv" {
                c.args([
                    "-attach",
                    &t.to_string_lossy(),
                    "-metadata:s:t:0",
                    "mimetype=image/jpeg",
                    "-metadata:s:t:0",
                    "filename=cover.jpg",
                ]);
            }
        }
    }
    if ["mp4", "mov"].contains(&opts.container.as_str()) {
        c.args(["-movflags", "+faststart+use_metadata_tags"]);
    }
    if opts.video_codec == "h265" && ["mp4", "mov"].contains(&opts.container.as_str()) {
        c.args(["-tag:v", "hvc1"]);
    }
    c.arg(&tmp);
    let code = stream_command(c, tx, "[ffmpeg] ");
    if code == 0 && tmp.is_file() && fs::metadata(&tmp).map(|m| m.len()).unwrap_or(0) > 0 {
        fs::rename(tmp, dst).is_ok()
    } else {
        let _ = fs::remove_file(tmp);
        false
    }
}

fn prepare_tools(tx: Sender<Event>) {
    let ff_ok = if ffmpeg("ffmpeg").is_some() && ffmpeg("ffprobe").is_some() {
        true
    } else {
        build_ffmpeg(&tx)
    };
    let yt_ok = ytdlp_command().is_some();
    if !yt_ok {
        let _ = tx.send(Event::Log(
            "yt-dlp was not found on PATH or in binaries/.".into(),
        ));
    }
    let _ = tx.send(Event::Startup(ff_ok, yt_ok));
}
fn build_ffmpeg(tx: &Sender<Event>) -> bool {
    let configure = ffmpeg_source().join("configure");
    if !configure.is_file() {
        let _ = tx.send(Event::Error(format!(
            "No FFmpeg source checkout was found at {}",
            ffmpeg_source().display()
        )));
        return false;
    }
    let out = binaries_dir().join("ffmpeg-build");
    let _ = fs::create_dir_all(&out);
    let mut cmd = Command::new(if cfg!(windows) { "bash" } else { "sh" });
    cmd.current_dir(&ffmpeg_source()).arg(&configure).args([
        "--prefix",
        out.to_string_lossy().as_ref(),
        "--disable-doc",
        "--disable-ffplay",
        "--enable-gpl",
        "--enable-version3",
    ]);
    if stream_command(cmd, tx, "[ffmpeg-build] ") != 0 {
        return false;
    }
    if stream_command(
        {
            let mut c = Command::new("make");
            c.current_dir(ffmpeg_source());
            c.arg(format!(
                "-j{}",
                std::thread::available_parallelism()
                    .map(|n| n.get())
                    .unwrap_or(4)
            ));
            c
        },
        tx,
        "[ffmpeg-build] ",
    ) != 0
    {
        return false;
    }
    stream_command(
        {
            let mut c = Command::new("make");
            c.current_dir(ffmpeg_source()).arg("install");
            c
        },
        tx,
        "[ffmpeg-build] ",
    ) == 0
        && ffmpeg("ffmpeg").is_some()
        && ffmpeg("ffprobe").is_some()
}
fn update_ffmpeg(tx: Sender<Event>) {
    let source = ffmpeg_source();
    let old = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(&source)
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string());
    let pull = Command::new("git")
        .args(["pull", "--ff-only"])
        .current_dir(&source)
        .output();
    let Ok(p) = pull else {
        let _ = tx.send(Event::Update(
            false,
            false,
            false,
            "Could not run Git.".into(),
        ));
        return;
    };
    if !p.status.success() {
        let _ = tx.send(Event::Update(
            false,
            false,
            false,
            String::from_utf8_lossy(&p.stderr).trim().into(),
        ));
        return;
    }
    let new = Command::new("git")
        .args(["rev-parse", "HEAD"])
        .current_dir(source)
        .output()
        .ok()
        .map(|o| String::from_utf8_lossy(&o.stdout).trim().to_string());
    let changed = old != new;
    let rebuilt = !changed || build_ffmpeg(&tx);
    let _ = tx.send(Event::Update(
        true,
        changed,
        rebuilt,
        if changed {
            "FFmpeg source was updated and rebuilt.".into()
        } else {
            "FFmpeg is already up to date.".into()
        },
    ));
}

struct App {
    settings: Settings,
    url: String,
    mode: String,
    container: String,
    video_codec: String,
    audio_codec: String,
    outdir: String,
    log: String,
    status: String,
    rx: Receiver<Event>,
    tx: Sender<Event>,
    running: bool,
    startup: bool,
    tools_ready: bool,
    show_settings: bool,
}
impl App {
    fn new() -> Self {
        let (settings, warning) = load_settings();
        let (tx, rx) = mpsc::channel();
        let tx2 = tx.clone();
        thread::spawn(move || prepare_tools(tx2));
        let mut log = format!("App started. Build: {VERSION}\n");
        if let Some(w) = &warning {
            log.push_str(&format!("WARNING: {w}\n"));
        }
        Self {
            outdir: settings.default_download_dir.clone(),
            settings,
            url: String::new(),
            mode: "video".into(),
            container: "mp4".into(),
            video_codec: "copy".into(),
            audio_codec: "copy".into(),
            log,
            status: "Preparing FFmpeg / yt-dlp".into(),
            rx,
            tx,
            running: false,
            startup: true,
            tools_ready: false,
            show_settings: false,
        }
    }
    fn add_log(&mut self, s: impl AsRef<str>) {
        self.log.push_str(s.as_ref());
        self.log.push('\n');
    }
    fn containers(&self) -> &'static [&'static str] {
        match self.mode.as_str() {
            "audio" => AUDIO_CONTAINERS,
            "thumbnail" => THUMBNAIL_CONTAINERS,
            _ => VIDEO_CONTAINERS,
        }
    }
    fn validate(&self) -> Result<(), String> {
        if self.mode == "video" {
            if !compatible_video(&self.container, &self.video_codec) {
                return Err(format!(
                    "Video codec '{}' is not compatible with {}.",
                    self.video_codec, self.container
                ));
            }
            if !compatible_audio("video", &self.container, &self.audio_codec) {
                return Err(format!(
                    "Audio codec '{}' is not compatible with {}.",
                    self.audio_codec, self.container
                ));
            }
        }
        if self.mode == "audio"
            && !native_audio(&self.container, &self.settings)
            && !compatible_audio("audio", &self.container, &self.audio_codec)
        {
            let recommended = match self.container.as_str() {
                "mp3" => "mpeg-1",
                "m4a" => "aac",
                "wav" => "lpcm",
                "flac" => "flac",
                "opus" => "opus",
                _ => "copy",
            };
            return Err(format!(
                "Audio codec '{}' is not compatible with {}. Use '{}' or 'copy'.",
                self.audio_codec, self.container, recommended
            ));
        }
        Ok(())
    }
    fn start(&mut self) {
        if self.running || self.startup || !self.tools_ready {
            return;
        }
        let urls: Vec<String> = self
            .url
            .split(';')
            .map(str::trim)
            .filter(|s| !s.is_empty())
            .map(String::from)
            .collect();
        if urls.is_empty() {
            self.add_log("ERROR: Enter at least one URL.");
            return;
        }
        if let Err(e) = fs::create_dir_all(&self.outdir) {
            self.add_log(format!("ERROR: {e}"));
            return;
        }
        if let Err(e) = self.validate() {
            self.add_log(format!("ERROR: {e}"));
            return;
        }
        let opts = Options {
            mode: self.mode.clone(),
            container: self.container.clone(),
            video_codec: self.video_codec.clone(),
            audio_codec: self.audio_codec.clone(),
            outdir: PathBuf::from(&self.outdir),
            settings: self.settings.clone(),
        };
        let tx = self.tx.clone();
        thread::spawn(move || run_download(urls, opts, tx));
        self.running = true;
        self.status = "Running".into();
        self.add_log("Worker started.");
    }
}
impl eframe::App for App {
    fn update(&mut self, ctx: &egui::Context, _frame: &mut eframe::Frame) {
        while let Ok(e) = self.rx.try_recv() {
            match e {
                Event::Log(s) => self.add_log(s),
                Event::Error(s) => self.add_log(format!("ERROR: {s}")),
                Event::Done(p) => {
                    self.running = false;
                    self.status = format!("Finished. Output folder: {}", p.display());
                }
                Event::Startup(a, b) => {
                    self.startup = false;
                    self.tools_ready = a && b;
                    self.status = if self.tools_ready {
                        "Idle".into()
                    } else {
                        "Tool setup failed".into()
                    };
                }
                Event::Update(a, c, r, s) => {
                    self.add_log(s);
                    self.add_log(format!(
                        "FFmpeg update: success={a}, changed={c}, rebuilt={r}"
                    ));
                }
            }
        }
        egui::CentralPanel::default().show(ctx, |ui| {
            ui.heading("YouTube Downloader");
            ui.horizontal(|ui| {
                ui.label("URL(s):");
                ui.text_edit_singleline(&mut self.url);
                if ui.button("Paste").clicked() {
                    if let Ok(mut cb) = arboard::Clipboard::new() {
                        if let Ok(t) = cb.get_text() {
                            self.url = t;
                        }
                    }
                }
            });
            ui.horizontal(|ui| {
                ui.label("Mode:");
                egui::ComboBox::from_id_salt("mode")
                    .selected_text(&self.mode)
                    .show_ui(ui, |ui| {
                        for x in ["video", "audio", "thumbnail"] {
                            ui.selectable_value(&mut self.mode, x.into(), x);
                        }
                    });
                ui.label("Container:");
                egui::ComboBox::from_id_salt("container")
                    .selected_text(&self.container)
                    .show_ui(ui, |ui| {
                        for x in self.containers() {
                            ui.selectable_value(&mut self.container, (*x).into(), *x);
                        }
                    });
                ui.label("Video codec:");
                egui::ComboBox::from_id_salt("vcodec")
                    .selected_text(&self.video_codec)
                    .show_ui(ui, |ui| {
                        for x in VIDEO_CODECS {
                            if self.mode == "video" && compatible_video(&self.container, x) {
                                ui.selectable_value(&mut self.video_codec, (*x).into(), *x);
                            }
                        }
                    });
                ui.label("Audio codec:");
                egui::ComboBox::from_id_salt("acodec")
                    .selected_text(&self.audio_codec)
                    .show_ui(ui, |ui| {
                        for x in AUDIO_CODECS {
                            if self.mode != "thumbnail" {
                                ui.selectable_value(&mut self.audio_codec, (*x).into(), *x);
                            }
                        }
                    });
            });
            ui.horizontal(|ui| {
                ui.label("Output folder:");
                ui.text_edit_singleline(&mut self.outdir);
                if ui.button("Browse").clicked() {
                    if let Some(p) = FileDialog::new().set_directory(&self.outdir).pick_folder() {
                        self.outdir = p.to_string_lossy().into();
                    }
                }
            });
            ui.horizontal(|ui| {
                if ui
                    .add_enabled(
                        !self.running && !self.startup && self.tools_ready,
                        egui::Button::new("Start"),
                    )
                    .clicked()
                {
                    self.start();
                }
                if ui.button("Open Output Folder").clicked() {
                    let _ = if cfg!(target_os = "windows") {
                        Command::new("explorer").arg(&self.outdir).spawn()
                    } else if cfg!(target_os = "macos") {
                        Command::new("open").arg(&self.outdir).spawn()
                    } else {
                        Command::new("xdg-open").arg(&self.outdir).spawn()
                    };
                }
                if ui.button("Settings").clicked() {
                    self.show_settings = true;
                }
                ui.label(&self.status);
            });
            egui::ScrollArea::vertical()
                .stick_to_bottom(true)
                .show(ui, |ui| {
                    ui.add(
                        egui::TextEdit::multiline(&mut self.log)
                            .desired_rows(24)
                            .font(egui::TextStyle::Monospace)
                            .interactive(false),
                    );
                });
        });
        if self.show_settings {
            egui::Window::new("Settings")
                .collapsible(false)
                .resizable(false)
                .show(ctx, |ui| {
                    egui::ComboBox::from_label("Encoder backend")
                        .selected_text(&self.settings.encoder_backend)
                        .show_ui(ui, |ui| {
                            for x in BACKENDS {
                                ui.selectable_value(
                                    &mut self.settings.encoder_backend,
                                    (*x).into(),
                                    *x,
                                );
                            }
                        });
                    ui.checkbox(
                        &mut self.settings.download_playlist,
                        "Download full playlist",
                    );
                    ui.checkbox(
                        &mut self.settings.use_ytdlp_audio_conversion,
                        "Use yt-dlp for audio conversion when supported",
                    );
                    ui.checkbox(
                        &mut self.settings.embed_metadata,
                        "Embed thumbnail, metadata, and chapters",
                    );
                    ui.horizontal(|ui| {
                        if ui.button("Save").clicked() {
                            match save_settings(&self.settings) {
                                Ok(_) => {
                                    self.outdir = self.settings.default_download_dir.clone();
                                    self.add_log("Settings saved.");
                                    self.show_settings = false;
                                }
                                Err(e) => self.add_log(format!("ERROR: {e}")),
                            }
                        }
                        if ui.button("Browse default directory").clicked() {
                            if let Some(p) = FileDialog::new().pick_folder() {
                                self.settings.default_download_dir = p.to_string_lossy().into();
                            }
                        }
                        if ui.button("Check / Update FFmpeg").clicked() {
                            let tx = self.tx.clone();
                            thread::spawn(move || update_ffmpeg(tx));
                        }
                    });
                    if ui.button("Reset to Defaults").clicked() {
                        self.settings = Settings::default();
                        self.outdir = self.settings.default_download_dir.clone();
                    }
                });
        }
        ctx.request_repaint_after(Duration::from_millis(100));
    }
}

fn main() -> eframe::Result {
    let options = eframe::NativeOptions {
        viewport: egui::ViewportBuilder::default()
            .with_min_inner_size([900.0, 520.0])
            .with_title("YouTube Downloader"),
        ..Default::default()
    };
    eframe::run_native(
        "YouTube Downloader",
        options,
        Box::new(|_| Ok(Box::new(App::new()))),
    )
}
