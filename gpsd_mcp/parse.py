"""Turn raw gpsd messages into small, stable dictionaries.

Field names follow the gpsd JSON reference: https://gpsd.gitlab.io/gpsd/gpsd_json.html
"""

from __future__ import annotations

import math
from datetime import datetime, timezone
from typing import Any, Mapping, Sequence

__all__ = [
    "constellation_name",
    "fix_mode_name",
    "haversine_m",
    "summarize_devices",
    "summarize_sky",
    "summarize_stream",
    "summarize_tpv",
]

FIX_MODES = {0: "unknown", 1: "no fix", 2: "2D fix", 3: "3D fix"}
CONSTELLATIONS = {0: "GPS", 1: "SBAS", 2: "Galileo", 3: "BeiDou", 4: "IMES", 5: "QZSS", 6: "GLONASS", 7: "NavIC"}
_EARTH_RADIUS_M = 6_371_008.8


def fix_mode_name(mode: Any) -> str:
    try:
        return FIX_MODES.get(int(mode), f"unknown ({mode})")
    except (TypeError, ValueError):
        return "unknown"


def constellation_name(gnssid: Any) -> str:
    try:
        return CONSTELLATIONS.get(int(gnssid), f"unknown ({gnssid})")
    except (TypeError, ValueError):
        return "unknown"


def _utc(value: Any) -> str | None:
    """Normalise a gpsd ISO-8601 timestamp, dropping sub-millisecond noise."""
    if not isinstance(value, str) or not value:
        return None
    text = value.strip()
    if text.endswith(("Z", "z")):
        text = text[:-1] + "+00:00"
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _age_seconds(value: Any) -> float | None:
    stamp = _utc(value)
    if stamp is None:
        return None
    parsed = datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    return round((datetime.now(timezone.utc) - parsed).total_seconds(), 3)


def _num(value: Any, digits: int = 3) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if math.isnan(number) or math.isinf(number):
        return None
    return round(number, digits)


def summarize_devices(devices: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Pick the interesting fields out of a ``DEVICES`` reply."""
    return [
        {"path": d.get("path"), "driver": d.get("driver"), "activated": _utc(d.get("activated"))}
        for d in devices
        if isinstance(d, Mapping)
    ]


def summarize_tpv(tpv: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise a ``TPV`` (time/position/velocity) message."""
    try:
        mode = int(tpv.get("mode", 0))
    except (TypeError, ValueError):
        mode = 0

    epx, epy = _num(tpv.get("epx")), _num(tpv.get("epy"))
    speed = _num(tpv.get("speed"))
    horizontal = round(math.hypot(epx, epy), 3) if epx is not None and epy is not None else None

    result: dict[str, Any] = {
        "time_utc": _utc(tpv.get("time")),
        "age_seconds": _age_seconds(tpv.get("time")),
        "mode": mode,
        "fix": fix_mode_name(mode),
        "has_fix": mode >= 2,
        "latitude_deg": _num(tpv.get("lat"), 7),
        "longitude_deg": _num(tpv.get("lon"), 7),
        "altitude_m": _num(tpv.get("alt")),
        "speed_mps": speed,
        "speed_kmh": round(speed * 3.6, 3) if speed is not None else None,
        "track_deg": _num(tpv.get("track"), 1),
        "climb_mps": _num(tpv.get("climb")),
        "accuracy": _drop_none(
            {
                "horizontal_m": horizontal,
                "vertical_m": _num(tpv.get("epv")),
                "speed_mps": _num(tpv.get("eps")),
            }
        ),
    }
    return _drop_none(result)


def summarize_sky(sky: Mapping[str, Any], *, only_used: bool = False) -> dict[str, Any]:
    """Normalise a ``SKY`` (satellite view + DOP) message."""
    satellites = []
    for sat in sky.get("satellites") or []:
        if not isinstance(sat, Mapping):
            continue
        used = bool(sat.get("used"))
        if only_used and not used:
            continue
        satellites.append(
            _drop_none(
                {
                    "prn": sat.get("PRN"),
                    "constellation": constellation_name(sat.get("gnssid")),
                    "elevation_deg": _num(sat.get("el"), 1),
                    "azimuth_deg": _num(sat.get("az"), 1),
                    "snr_db_hz": _num(sat.get("ss"), 1),
                    "used": used,
                }
            )
        )
    # Strongest, used satellites first: the ones that matter for a fix.
    satellites.sort(key=lambda s: (not s.get("used"), -(s.get("snr_db_hz") or 0.0)))

    return _drop_none(
        {
            "time_utc": _utc(sky.get("time")),
            "satellites_visible": sky.get("nSat"),
            "satellites_used": sky.get("uSat"),
            "dop": _drop_none(
                {
                    name: _num(sky.get(name), 2)
                    for name in ("gdop", "pdop", "hdop", "vdop", "tdop")
                }
            ),
            "satellites": satellites,
        }
    )


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in metres between two WGS-84 points."""
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    d_phi, d_lambda = phi2 - phi1, math.radians(lon2 - lon1)
    a = math.sin(d_phi / 2) ** 2 + math.cos(phi1) * math.cos(phi2) * math.sin(d_lambda / 2) ** 2
    return 2 * _EARTH_RADIUS_M * math.asin(min(1.0, math.sqrt(a)))


def summarize_stream(samples: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Aggregate a list of :func:`summarize_tpv` results."""
    if not samples:
        return {"sample_count": 0, "valid_fixes": 0, "message": "no TPV samples arrived"}

    fixes = [s for s in samples if s.get("has_fix")]
    distance = sum(
        haversine_m(a["latitude_deg"], a["longitude_deg"], b["latitude_deg"], b["longitude_deg"])
        for a, b in zip(fixes, fixes[1:])
    )
    speeds = [s["speed_mps"] for s in fixes if s.get("speed_mps") is not None]
    altitudes = [s["altitude_m"] for s in fixes if s.get("altitude_m") is not None]

    return _drop_none(
        {
            "sample_count": len(samples),
            "valid_fixes": len(fixes),
            "fix_ratio": round(len(fixes) / len(samples), 3),
            "first_time_utc": samples[0].get("time_utc"),
            "last_time_utc": samples[-1].get("time_utc"),
            "distance_m": round(distance, 2) if len(fixes) > 1 else None,
            "speed_avg_kmh": round(sum(speeds) / len(speeds) * 3.6, 2) if speeds else None,
            "speed_max_kmh": round(max(speeds) * 3.6, 2) if speeds else None,
            "altitude_min_m": round(min(altitudes), 2) if altitudes else None,
            "altitude_max_m": round(max(altitudes), 2) if altitudes else None,
        }
    )


def _drop_none(mapping: Mapping[str, Any]) -> dict[str, Any]:
    """Keep payloads small: absent gpsd fields stay absent."""
    return {key: value for key, value in mapping.items() if value is not None}
