from __future__ import annotations

import argparse
import csv
import html
import json
import shutil
import subprocess
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .telemetry import AnalysisSample, Reading, analyze_samples, parse_gpx, parse_timestamp, reading_at, video_timestamp


_GEOCODE_CACHE: dict[tuple[float, float], str] = {}


def main() -> None:
    parser = argparse.ArgumentParser(description="Align GPX data and render cycling telemetry overlays.")
    subparsers = parser.add_subparsers(dest="command", required=True)
    align = subparsers.add_parser("align", help="Calculate the fixed GPX-to-video offset from a matching moment.")
    align.add_argument("--video-moment", required=True, help="Video timestamp at a matching event (ISO 8601).")
    align.add_argument("--gpx-moment", required=True, help="GPX timestamp at that same event (ISO 8601).")
    align.add_argument("--output", type=Path, default=Path("alignment.json"))
    render = subparsers.add_parser("render", help="Render a telemetry dashboard into a video.")
    render.add_argument("video", type=Path)
    render.add_argument("--gpx", type=Path, required=True)
    render.add_argument("--video-start", help="Video capture start timestamp (ISO 8601); inferred from metadata when omitted.")
    render.add_argument("--offset-file", type=Path, default=Path("alignment.json"))
    render.add_argument("--output", type=Path)
    render.add_argument("--sample-rate", type=float, default=2.0, help="Dashboard updates per second (default: 2).")
    folder = subparsers.add_parser("render-folder", help="Render every supported video in a folder.")
    folder.add_argument("folder", type=Path)
    folder.add_argument("--gpx", type=Path, required=True)
    folder.add_argument("--offset-file", type=Path, default=Path("alignment.json"))
    folder.add_argument("--output-dir", type=Path)
    folder.add_argument("--sample-rate", type=float, default=2.0, help="Dashboard updates per second (default: 2).")
    analyze = subparsers.add_parser("analyze", help="Create a visual report of timestamp, speed, and direction samples.")
    analyze.add_argument("--gpx", type=Path, required=True)
    analyze.add_argument("--output", type=Path, default=Path("gpx-analysis.html"))
    analyze.add_argument("--csv", type=Path, help="Optional CSV export of the same samples.")
    timestamps = subparsers.add_parser("video-timestamps", help="Report a video's metadata timestamp and timing range.")
    timestamps.add_argument("video", type=Path)
    arguments = parser.parse_args()
    if arguments.command == "align":
        align_timestamps(arguments)
    elif arguments.command == "render":
        render_video(arguments)
    elif arguments.command == "analyze":
        analyze_gpx(arguments)
    elif arguments.command == "video-timestamps":
        show_video_timestamps(arguments)
    else:
        render_folder(arguments)


def align_timestamps(arguments: argparse.Namespace) -> None:
    offset_s = (parse_timestamp(arguments.gpx_moment) - parse_timestamp(arguments.video_moment)).total_seconds()
    arguments.output.write_text(json.dumps({"offset_seconds": offset_s}, indent=2) + "\n", encoding="utf-8")
    print(f"Saved offset: {offset_s:+.3f} seconds (GPX = video + offset) to {arguments.output}")


def render_video(arguments: argparse.Namespace) -> None:
    _require_ffmpeg()
    points = parse_gpx(arguments.gpx)
    offset_s = json.loads(arguments.offset_file.read_text(encoding="utf-8"))["offset_seconds"]
    start = parse_timestamp(arguments.video_start) if arguments.video_start else _video_creation_time(arguments.video)
    duration_s = _video_duration(arguments.video)
    location_timestamp = video_timestamp(start, 0, offset_s)
    location_point = min(points, key=lambda point: abs((point.timestamp - location_timestamp).total_seconds()))
    location = _reverse_geocode(location_point.latitude, location_point.longitude)
    subtitle_path = (arguments.output or arguments.video.with_stem(arguments.video.stem + "_telemetry")).with_suffix(".ass")
    output = arguments.output or arguments.video.with_stem(arguments.video.stem + "_telemetry.mp4")
    _write_subtitles(subtitle_path, points, start, offset_s, duration_s, arguments.sample_rate, location)
    escaped_subtitle_path = subtitle_path.resolve().as_posix().replace(":", "\\:")
    escaped_fonts_dir = (Path(__file__).parent / "fonts").resolve().as_posix().replace(":", "\\:")
    subtitle_filter = f"subtitles=filename='{escaped_subtitle_path}':fontsdir='{escaped_fonts_dir}'"
    command = [
        "ffmpeg",
        "-y",
        "-i",
        str(arguments.video),
        "-map",
        "0:v:0",
        "-map",
        "0:a?",
        "-sn",
        "-dn",
        "-map_metadata",
        "-1",
        "-map_chapters",
        "-1",
        "-vf",
        subtitle_filter,
        "-c:v",
        "libx264",
        "-crf",
        "18",
        "-preset",
        "medium",
        "-c:a",
        "copy",
        str(output),
    ]
    print("Running:", " ".join(command))
    try:
        subprocess.run(command, check=True)
    finally:
        subtitle_path.unlink(missing_ok=True)
    print(f"Created {output}")


def render_folder(arguments: argparse.Namespace) -> None:
    output_dir = arguments.output_dir or arguments.folder / "telemetry"
    output_dir.mkdir(parents=True, exist_ok=True)
    videos = sorted({path for extension in ("*.mp4", "*.mov", "*.m4v", "*.MP4", "*.MOV", "*.M4V") for path in arguments.folder.glob(extension)})
    if not videos:
        raise ValueError(f"No MP4, MOV, or M4V videos found in {arguments.folder}.")
    for video in videos:
        output = output_dir / f"{video.stem}_telemetry.mp4"
        if output.exists():
            print(f"Skipping {video}: output already exists at {output}")
            continue
        render_video(argparse.Namespace(video=video, gpx=arguments.gpx, video_start=None, offset_file=arguments.offset_file, output=output, sample_rate=arguments.sample_rate))


def analyze_gpx(arguments: argparse.Namespace) -> None:
    samples = analyze_samples(parse_gpx(arguments.gpx))
    arguments.output.write_text(_analysis_html(samples), encoding="utf-8")
    if arguments.csv:
        with arguments.csv.open("w", newline="", encoding="utf-8") as output_file:
            writer = csv.writer(output_file)
            writer.writerow(("timestamp", "speed_kph", "direction_degrees", "direction"))
            for sample in samples:
                writer.writerow(_sample_row(sample))
    print(f"Created {arguments.output} with {len(samples)} samples")
    if arguments.csv:
        print(f"Created {arguments.csv}")


def show_video_timestamps(arguments: argparse.Namespace) -> None:
    _require_ffmpeg()
    start = _video_creation_time(arguments.video)
    duration_s = _video_duration(arguments.video)
    end = start + timedelta(seconds=duration_s)
    print(f"File: {arguments.video}")
    print(f"Metadata start: {start.isoformat()}")
    print(f"Duration: {duration_s:.3f} seconds")
    print(f"Calculated end: {end.isoformat()}")


def _sample_row(sample: AnalysisSample) -> tuple[str, str, str, str]:
    return (
        sample.timestamp.isoformat(),
        f"{sample.speed_kph:.2f}" if sample.speed_kph is not None else "",
        f"{sample.direction_degrees:.1f}" if sample.direction_degrees is not None else "",
        sample.direction_label or "",
    )


def _analysis_html(samples: list[AnalysisSample]) -> str:
    rows = "\n".join(
        f"<tr><td>{html.escape(timestamp)}</td><td>{speed or '--'}</td><td>{bearing or '--'}</td><td>{direction or '--'}</td></tr>"
        for timestamp, speed, bearing, direction in (_sample_row(sample) for sample in samples)
    )
    values = [sample.speed_kph for sample in samples if sample.speed_kph is not None]
    maximum_speed = max(values, default=0)
    chart = "".join(
        f'<div class="bar" title="{html.escape(sample.timestamp.isoformat())}: {sample.speed_kph:.1f} km/h" style="height:{max(2, round(sample.speed_kph / maximum_speed * 100)) if maximum_speed else 2}%"></div>'
        for sample in samples if sample.speed_kph is not None
    )
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GPX Sample Analyzer</title><style>
:root {{ color-scheme: light; --ink:#17221d; --paper:#f4f2e8; --accent:#e3502d; --line:#c8c7bd; }}
* {{ box-sizing:border-box; }} body {{ margin:0; background:var(--paper); color:var(--ink); font:15px Georgia,serif; }}
main {{ max-width:1200px; margin:auto; padding:40px 24px; }} h1 {{ margin:0; font:700 42px/1.1 Georgia,serif; }} p {{ color:#536057; }}
.chart {{ height:180px; display:flex; align-items:end; gap:1px; margin:24px 0 36px; border-bottom:1px solid var(--ink); overflow:hidden; }}
.bar {{ background:var(--accent); min-width:2px; flex:1; }} input {{ width:100%; max-width:420px; padding:10px; border:1px solid var(--line); background:#fffdf6; font:inherit; }}
table {{ width:100%; border-collapse:collapse; margin-top:16px; background:#fffdf6; }} th,td {{ text-align:left; padding:10px; border-bottom:1px solid var(--line); white-space:nowrap; }} th {{ font-family:Arial,sans-serif; font-size:12px; letter-spacing:0; text-transform:uppercase; }}
@media (max-width:600px) {{ main {{ padding:28px 14px; }} h1 {{ font-size:32px; }} table {{ font-size:13px; }} th,td {{ padding:8px; }} }}
</style></head><body><main><h1>GPX Sample Analyzer</h1><p>{len(samples)} timed samples. Speed derives from the preceding GPS segment; bearing is clockwise from true north.</p>
<section class="chart" aria-label="Speed chart">{chart}</section><input id="filter" type="search" placeholder="Filter timestamp, speed, or direction" aria-label="Filter samples">
<table><thead><tr><th>Timestamp</th><th>Speed (km/h)</th><th>Bearing</th><th>Direction</th></tr></thead><tbody id="samples">{rows}</tbody></table>
</main><script>document.querySelector('#filter').addEventListener('input', event => document.querySelectorAll('#samples tr').forEach(row => row.hidden = !row.textContent.toLowerCase().includes(event.target.value.toLowerCase())));</script></body></html>"""


def _write_subtitles(path: Path, points: list, start: datetime, offset_s: float, duration_s: float, sample_rate: float, location: str) -> None:
    if sample_rate <= 0:
        raise ValueError("--sample-rate must be greater than zero.")
    interval_s = 1 / sample_rate
    lines = [_ass_header(), _dialogue(1, "0:00:00.00", _ass_time(duration_s), "Location", f"{{\\an7\\pos(20,25)}}{location}")]
    position_s = 0.0
    while position_s < duration_s:
        gpx_timestamp = video_timestamp(start, position_s, offset_s)
        reading = reading_at(points, gpx_timestamp)
        if reading:
            event_start = _ass_time(position_s)
            event_end = _ass_time(min(position_s + interval_s, duration_s))
            lines.extend(_dashboard_events(event_start, event_end, reading, gpx_timestamp))
        position_s += interval_s
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def _reverse_geocode(latitude: float, longitude: float) -> str:
    cache_key = (round(latitude, 4), round(longitude, 4))
    if cache_key in _GEOCODE_CACHE:
        return _GEOCODE_CACHE[cache_key]
    query = urlencode({"lat": latitude, "lon": longitude, "format": "jsonv2", "addressdetails": 1})
    request = Request(
        f"https://nominatim.openstreetmap.org/reverse?{query}",
        headers={"User-Agent": "ride-overlay/0.1 (local telemetry renderer)"},
    )
    try:
        with urlopen(request, timeout=10) as response:
            address = json.load(response).get("address", {})
    except (OSError, URLError, json.JSONDecodeError):
        location = f"GPS {latitude:.5f}, {longitude:.5f}"
    else:
        names = []
        for key in ("village", "town", "city", "municipality", "district", "county"):
            name = address.get(key)
            if name and name not in names:
                names.append(name)
        location = ", ".join(names[:3]) or f"GPS {latitude:.5f}, {longitude:.5f}"
    _GEOCODE_CACHE[cache_key] = location
    print(f"Location: {location}")
    return location


def _dashboard_events(start: str, end: str, reading: Reading, gpx_timestamp: datetime | None = None) -> list[str]:
    gradient = f"{reading.gradient_percent:.1f}" if reading.gradient_percent is not None else "---.-"
    heading = f"{reading.direction_degrees:03.0f}" if reading.direction_degrees is not None else "---"
    heart_rate = f"{reading.heart_rate_bpm:03d}" if reading.heart_rate_bpm is not None else "---"
    temperature = f"TEMP {reading.temperature_c:.1f} C" if reading.temperature_c is not None else "TEMP --.- C"
    utc_plus_8 = timezone(timedelta(hours=8))
    clock = gpx_timestamp.astimezone(utc_plus_8).strftime("%H:%M:%S") if gpx_timestamp else "--:--:--"
    values = (
        (20, clock),
        (400, gradient),
        (675, f"{reading.distance_km:05.2f}"),
        (990, f"{min(max(reading.speed_kph, 0), 99.9):04.1f}"),
        (1_340, heading),
        (1_625, heart_rate),
    )
    return [
        _dialogue(0, start, end, "Bar", r"{\an7\pos(0,0)\p1\1c&H000000&\1a&H80&}m 0 957 l 1920 957 l 1920 1080 l 0 1080 c"),
        _dialogue(1, start, end, "Location", f"{{\\an7\\pos(20,65)}}{temperature}"),
        *(_dialogue(2, start, end, "Digits", f"{{\\an7\\pos({x},1000)}}{value}") for x, value in values),
        _dialogue(3, start, end, "Metric", r"{\an7\pos(415,975)}GRADE  %"),
        _dialogue(3, start, end, "Metric", r"{\an7\pos(695,975)}DISTANCE  KM"),
        _dialogue(3, start, end, "Metric", r"{\an7\pos(1000,975)}SPEED  KM/H"),
        _dialogue(3, start, end, "Metric", r"{\an7\pos(1340,975)}HEADING  DEG"),
        _dialogue(3, start, end, "Metric", r"{\an7\pos(1660,975)}HR  BPM"),
    ]


def _dialogue(layer: int, start: str, end: str, style: str, text: str) -> str:
    return f"Dialogue: {layer},{start},{end},{style},,0,0,0,,{text}"


def _ass_header() -> str:
    return """[Script Info]
ScriptType: v4.00+
PlayResX: 1920
PlayResY: 1080

[V4+ Styles]
Format: Name,Fontname,Fontsize,PrimaryColour,SecondaryColour,OutlineColour,BackColour,Bold,Italic,Underline,StrikeOut,ScaleX,ScaleY,Spacing,Angle,BorderStyle,Outline,Shadow,Alignment,MarginL,MarginR,MarginV,Encoding
Style: Bar,DejaVu Sans,36,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Digits,DSEG7 Classic,52,&H007AFF63,&H000000FF,&H00000000,&H00000000,0,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Metric,DejaVu Sans,20,&H0075BB6D,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1
Style: Location,DejaVu Sans,26,&H00FFFFFF,&H000000FF,&H00000000,&H00000000,1,0,0,0,100,100,0,0,1,0,0,7,0,0,0,1

[Events]
Format: Layer,Start,End,Style,Name,MarginL,MarginR,MarginV,Effect,Text"""


def _ass_time(seconds: float) -> str:
    centiseconds = round(seconds * 100)
    hours, remaining = divmod(centiseconds, 360_000)
    minutes, remaining = divmod(remaining, 6_000)
    return f"{hours}:{minutes:02}:{remaining // 100:02}.{remaining % 100:02}"


def _require_ffmpeg() -> None:
    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        raise RuntimeError("ffmpeg and ffprobe must be on PATH.")


def _probe(video: Path, field: str) -> str:
    command = ["ffprobe", "-v", "error", "-show_entries", field, "-of", "default=noprint_wrappers=1:nokey=1", str(video)]
    return subprocess.check_output(command, text=True).strip()


def _video_duration(video: Path) -> float:
    return float(_probe(video, "format=duration"))


def _video_creation_time(video: Path) -> datetime:
    value = _probe(video, "format_tags=creation_time")
    if not value:
        raise ValueError("Video creation time is missing. Supply --video-start in ISO 8601 format.")
    return parse_timestamp(value)


if __name__ == "__main__":
    main()