from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import requests

from ..config import Settings
from ..models import WeatherEnsembleSample


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_from_member_key(member_key: str) -> str:
    normalized = member_key.lower()
    if "gfs" in normalized:
        return "gfs_ensemble"
    if "ecmwf" in normalized:
        return "ecmwf_ensemble"
    return "ensemble"


def _parse_local_time(time_value: str, tz_name: str) -> datetime | None:
    candidate = time_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    local_tz = ZoneInfo(tz_name)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=local_tz)
    return parsed.astimezone(local_tz)


def fetch_weather_ensemble_samples(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    now_utc: datetime | None = None,
) -> list[WeatherEnsembleSample]:
    current_utc = now_utc or datetime.now(timezone.utc)
    local_tz = ZoneInfo(settings.weather_timezone)
    target_date = current_utc.astimezone(local_tz).date()

    params = {
        "latitude": settings.weather_latitude,
        "longitude": settings.weather_longitude,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "models": ",".join(settings.weather_ensemble_models),
        "forecast_days": settings.weather_forecast_days,
        "timezone": settings.weather_timezone,
    }
    client = session or requests.Session()
    response = client.get("https://api.open-meteo.com/v1/ensemble", params=params, timeout=20)
    response.raise_for_status()
    payload = response.json()

    hourly = payload.get("hourly", {})
    times = hourly.get("time", [])
    if not isinstance(times, list) or not times:
        return []

    member_keys = [
        key
        for key, values in hourly.items()
        if key != "time" and key.lower().startswith("temperature_2m") and isinstance(values, list)
    ]
    samples: list[WeatherEnsembleSample] = []
    for member_key in member_keys:
        values = hourly.get(member_key, [])
        if len(values) != len(times):
            continue
        day_max: float | None = None
        for idx, time_value in enumerate(times):
            if not isinstance(time_value, str):
                continue
            local_dt = _parse_local_time(time_value, settings.weather_timezone)
            if local_dt is None or local_dt.date() != target_date:
                continue
            reading = _as_float(values[idx])
            if reading is None:
                continue
            if day_max is None or reading > day_max:
                day_max = reading
        if day_max is None:
            continue
        samples.append(
            WeatherEnsembleSample(
                collected_at=current_utc,
                target_date=target_date,
                model=_model_from_member_key(member_key),
                member=member_key,
                max_temp_f=day_max,
                source="open-meteo",
                raw_json={"member_key": member_key},
            )
        )
    return samples

