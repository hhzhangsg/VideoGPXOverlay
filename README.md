# Ride Overlay

Render speed, distance, heart rate, and grade from a Garmin GPX file over cycling video. It uses a single, fixed clock correction for the whole ride:

`GPX time = video capture time + offset`

## Prerequisites

- Python 3.11+
- [FFmpeg](https://ffmpeg.org/) and `ffprobe` available on `PATH`

Install locally from the project folder:

```powershell
python -m pip install -e .
```

## 1. Calibrate the clocks

Find one recognisable moment visible in a video (for example, starting a climb) and identify its timestamp in the GPX timeline. Enter both timestamps in ISO 8601 form:

```powershell
ride-overlay align --video-moment "2026-09-04T20:22:25+00:00" --gpx-moment "2026-09-04T20:20:52+00:00"
```

This writes `alignment.json`. A positive offset means GPX timestamps are later than video timestamps.

## 2. Render a video

When the file has a `creation_time` tag:

```powershell
ride-overlay render "C:\rides\clip001.mp4" --gpx "C:\rides\activity.gpx"
```

When it does not, pass the capture start time explicitly:

```powershell
ride-overlay render "C:\rides\clip001.mp4" --gpx "C:\rides\activity.gpx" --video-start "2026-09-07T08:30:00+00:00"
```

The output is next to the input as `clip001_telemetry.mp4`. The dashboard updates twice each second in a compact, solid-black bottom bar. Speed, total distance, gradient, heading, and heart rate use the bundled green [DSEG7 Classic](https://github.com/keshikan/DSEG) display font, licensed under SIL OFL 1.1; see `ride_overlay/fonts/DSEG-LICENSE.txt`. Use `--sample-rate 1` for a smaller subtitle file, or `--output` to choose an output path.

The renderer reverse-geocodes the GPX location nearest the clip start through OpenStreetMap's Nominatim service and displays the village, town, city, district, or county name in the upper-left corner. Internet access is optional; if the lookup is unavailable, the overlay displays the GPX coordinates instead.

For a folder of MP4, MOV, or M4V clips that retain their capture-time metadata:

```powershell
ride-overlay render-folder "C:\rides\raw" --gpx "C:\rides\activity.gpx"
```

Outputs are placed in `C:\rides\raw\telemetry`. All clips use the same alignment file, which is appropriate when their camera clocks share the same offset.
Existing output files are skipped, so the batch command can be safely rerun to continue unfinished processing.

## Analyze GPX samples

Create a self-contained visual HTML report with the timestamp, calculated speed, and compass direction for every timed GPX point. Optionally export the same data as CSV:

```powershell
ride-overlay analyze --gpx "C:\rides\activity.gpx" --output "C:\rides\gpx-analysis.html" --csv "C:\rides\gpx-samples.csv"
```

Open the generated HTML report in a browser. It includes a speed chart and a filterable sample table. Direction is the calculated travel bearing clockwise from true north; the first point has no speed or direction because it has no preceding segment.

## Inspect Video Timestamps

Report the video container's creation timestamp, duration, and calculated end timestamp:

```powershell
ride-overlay video-timestamps "C:\rides\VID_20260905_022948_745.mp4"
```

This uses the embedded `creation_time` metadata. If it is missing, the command reports that a manual `--video-start` timestamp is needed when rendering.

## Notes

- GPX speed is calculated from consecutive GPS points; grade is elevation change over a local 10-point window to reduce GPS noise.
- Garmin Connect GPX exports may omit heart rate. Export FIT/TCX and convert it to GPX with extensions if heart rate is absent.
- Video before or after the GPX activity gets no telemetry overlay.