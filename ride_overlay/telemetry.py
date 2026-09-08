from __future__ import annotations

from bisect import bisect_left
from dataclasses import dataclass
from datetime import datetime, timedelta
from math import asin, atan2, cos, degrees, radians, sin, sqrt
from pathlib import Path
from xml.etree import ElementTree


@dataclass(frozen=True)
class TrackPoint:
    timestamp: datetime
    latitude: float
    longitude: float
    elevation_m: float | None
    heart_rate_bpm: int | None
    distance_m: float
    temperature_c: float | None = None


@dataclass(frozen=True)
class Reading:
    speed_kph: float
    distance_km: float
    heart_rate_bpm: int | None
    gradient_percent: float | None
    direction_degrees: float | None
    direction_label: str | None
    temperature_c: float | None = None


@dataclass(frozen=True)
class AnalysisSample:
    timestamp: datetime
    speed_kph: float | None
    direction_degrees: float | None
    direction_label: str | None


def parse_timestamp(value: str) -> datetime:
    return datetime.fromisoformat(value.strip().replace("Z", "+00:00"))


def parse_gpx(path: Path) -> list[TrackPoint]:
    root = ElementTree.parse(path).getroot()
    raw_points = root.findall(".//{*}trkpt")
    if not raw_points:
        raise ValueError("The GPX file contains no track points.")

    points: list[TrackPoint] = []
    distance_m = 0.0
    previous: TrackPoint | None = None
    for element in raw_points:
        time_element = element.find("{*}time")
        if time_element is None or not time_element.text:
            continue
        elevation = _float_value(element.find("{*}ele"))
        heart_rate = _int_value(element.find(".//{*}hr"))
        temperature_c = _float_value(element.find(".//{*}atemp"))
        point = TrackPoint(
            timestamp=parse_timestamp(time_element.text),
            latitude=float(element.attrib["lat"]),
            longitude=float(element.attrib["lon"]),
            elevation_m=elevation,
            heart_rate_bpm=heart_rate,
            distance_m=distance_m,
            temperature_c=temperature_c,
        )
        if previous:
            distance_m += _haversine_m(previous, point)
            point = TrackPoint(**{**point.__dict__, "distance_m": distance_m})
        points.append(point)
        previous = point
    if len(points) < 2:
        raise ValueError("The GPX file needs at least two timed track points.")
    return points


def reading_at(points: list[TrackPoint], timestamp: datetime) -> Reading | None:
    timestamps = [point.timestamp for point in points]
    index = bisect_left(timestamps, timestamp)
    if index == 0 or index == len(points):
        return None
    before, after = points[index - 1], points[index]
    interval_s = (after.timestamp - before.timestamp).total_seconds()
    if interval_s <= 0:
        return None
    ratio = (timestamp - before.timestamp).total_seconds() / interval_s
    distance_m = _interpolate(before.distance_m, after.distance_m, ratio)
    speed_kph = (after.distance_m - before.distance_m) / interval_s * 3.6
    heart_rate = _interpolate_optional(before.heart_rate_bpm, after.heart_rate_bpm, ratio)
    temperature_c = _interpolate_optional_float(before.temperature_c, after.temperature_c, ratio)
    gradient = _gradient(points, index)
    bearing = _bearing_degrees(before, after) if after.distance_m > before.distance_m else None
    return Reading(speed_kph, distance_m / 1000, heart_rate, gradient, bearing, _compass_label(bearing), temperature_c)


def video_timestamp(video_start: datetime, position_s: float, offset_s: float) -> datetime:
    """Map a video position to GPX time, using GPX = video + offset."""
    return video_start + timedelta(seconds=position_s + offset_s)


def analyze_samples(points: list[TrackPoint]) -> list[AnalysisSample]:
    """Return one speed and compass direction sample for each timed GPX point."""
    samples = [AnalysisSample(points[0].timestamp, None, None, None)]
    for previous, current in zip(points, points[1:]):
        elapsed_s = (current.timestamp - previous.timestamp).total_seconds()
        speed_kph = (current.distance_m - previous.distance_m) / elapsed_s * 3.6 if elapsed_s > 0 else None
        bearing = _bearing_degrees(previous, current) if current.distance_m > previous.distance_m else None
        samples.append(AnalysisSample(current.timestamp, speed_kph, bearing, _compass_label(bearing)))
    return samples


def _float_value(element: ElementTree.Element | None) -> float | None:
    return float(element.text) if element is not None and element.text else None


def _int_value(element: ElementTree.Element | None) -> int | None:
    return round(float(element.text)) if element is not None and element.text else None


def _haversine_m(first: TrackPoint, second: TrackPoint) -> float:
    radius_m = 6_371_000
    latitude_delta = radians(second.latitude - first.latitude)
    longitude_delta = radians(second.longitude - first.longitude)
    a = sin(latitude_delta / 2) ** 2 + cos(radians(first.latitude)) * cos(radians(second.latitude)) * sin(longitude_delta / 2) ** 2
    return 2 * radius_m * asin(sqrt(a))


def _bearing_degrees(first: TrackPoint, second: TrackPoint) -> float:
    longitude_delta = radians(second.longitude - first.longitude)
    first_latitude = radians(first.latitude)
    second_latitude = radians(second.latitude)
    y = sin(longitude_delta) * cos(second_latitude)
    x = cos(first_latitude) * sin(second_latitude) - sin(first_latitude) * cos(second_latitude) * cos(longitude_delta)
    return (degrees(atan2(y, x)) + 360) % 360


def _compass_label(bearing: float | None) -> str | None:
    if bearing is None:
        return None
    directions = ("N", "NE", "E", "SE", "S", "SW", "W", "NW")
    return directions[round(bearing / 45) % len(directions)]


def _interpolate(first: float, second: float, ratio: float) -> float:
    return first + (second - first) * ratio


def _interpolate_optional(first: int | None, second: int | None, ratio: float) -> int | None:
    if first is None and second is None:
        return None
    return round(_interpolate(float(first or second), float(second or first), ratio))


def _interpolate_optional_float(first: float | None, second: float | None, ratio: float) -> float | None:
    if first is None and second is None:
        return None
    return _interpolate(float(first if first is not None else second), float(second if second is not None else first), ratio)


def _gradient(points: list[TrackPoint], center: int) -> float | None:
    start = max(0, center - 5)
    end = min(len(points) - 1, center + 5)
    first, last = points[start], points[end]
    if first.elevation_m is None or last.elevation_m is None:
        return None
    distance_m = last.distance_m - first.distance_m
    return (last.elevation_m - first.elevation_m) / distance_m * 100 if distance_m >= 5 else None