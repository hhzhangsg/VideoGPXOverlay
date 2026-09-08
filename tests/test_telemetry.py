from datetime import datetime, timezone
import unittest

from ride_overlay.cli import _dashboard_events
from ride_overlay.telemetry import TrackPoint, analyze_samples, reading_at, video_timestamp


def point(second: int, latitude: float, elevation: float, distance: float, heart_rate: int, temperature: float | None = None) -> TrackPoint:
    return TrackPoint(datetime(2026, 1, 1, 12, 0, second, tzinfo=timezone.utc), latitude, 0, elevation, heart_rate, distance, temperature)


class TelemetryTests(unittest.TestCase):
    def test_interpolates_reading_and_gradient(self) -> None:
        points = [point(0, 0, 100, 0, 120, 20), point(10, 0.001, 110, 100, 140, 22), point(20, 0.002, 120, 200, 160, 24)]
        reading = reading_at(points, datetime(2026, 1, 1, 12, 0, 5, tzinfo=timezone.utc))
        self.assertIsNotNone(reading)
        assert reading is not None
        self.assertEqual(reading.speed_kph, 36)
        self.assertEqual(reading.distance_km, 0.05)
        self.assertEqual(reading.heart_rate_bpm, 130)
        self.assertEqual(reading.temperature_c, 21)
        self.assertEqual(reading.gradient_percent, 10)
        self.assertEqual(reading.direction_label, "N")
        self.assertAlmostEqual(reading.direction_degrees or 0, 0, places=2)
        dashboard = "\n".join(_dashboard_events("0:00:00.00", "0:00:00.50", reading))
        self.assertIn("DISTANCE  KM", dashboard)
        self.assertIn("GRADE", dashboard)
        self.assertIn("HEADING", dashboard)
        self.assertIn("KM/H", dashboard)
        self.assertIn("TEMP 21.0 C", dashboard)
        self.assertIn("Digits", dashboard)
        self.assertIn(r"\pos(0,0)\p1", dashboard)
        self.assertIn(r"\1c&H000000&\1a&H80&", dashboard)

    def test_maps_video_time_with_offset(self) -> None:
        start = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)
        self.assertEqual(video_timestamp(start, 15, -3), datetime(2026, 1, 1, 12, 0, 12, tzinfo=timezone.utc))

    def test_analyzes_speed_and_direction_per_point(self) -> None:
        points = [point(0, 0, 100, 0, 120), point(10, 0.001, 100, 100, 120)]
        samples = analyze_samples(points)
        self.assertIsNone(samples[0].speed_kph)
        self.assertEqual(samples[1].speed_kph, 36)
        self.assertEqual(samples[1].direction_label, "N")
        self.assertAlmostEqual(samples[1].direction_degrees or 0, 0, places=2)