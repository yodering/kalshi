from __future__ import annotations

from datetime import datetime, timezone
import logging
from zoneinfo import ZoneInfo

import requests

from ..config import Settings
from ..models import WeatherEnsembleSample

logger = logging.getLogger(__name__)


def _as_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _model_from_member_key(member_key: str) -> str:
    normalized = member_key.lower()
    if normalized == "temperature_2m":
        return "best_match"
    if "gfs" in normalized:
        return "gfs_ensemble"
    if "ecmwf" in normalized:
        return "ecmwf_ensemble"
    if "icon" in normalized:
        return "icon"
    if "gem" in normalized:
        return "gem"
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


def _forecast_models_from_ensemble_models(models: list[str]) -> str:
    mapped: list[str] = []
    for model in models:
        normalized = model.strip().lower()
        if not normalized:
            continue
        if normalized == "gfs_ensemble":
            mapped.append("gfs_seamless")
            continue
        if normalized == "ecmwf_ifs025_ensemble":
            mapped.append("ecmwf_ifs025")
            continue
        mapped.append(normalized.replace("_ensemble", ""))
    if not mapped:
        mapped = ["best_match", "gfs_seamless", "ecmwf_ifs025"]
    # preserve order while deduping
    deduped = list(dict.fromkeys(mapped))
    return ",".join(deduped)


def fetch_weather_ensemble_samples(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    now_utc: datetime | None = None,
) -> list[WeatherEnsembleSample]:
    current_utc = now_utc or datetime.now(timezone.utc)
    local_tz = ZoneInfo(settings.weather_timezone)
    target_date = current_utc.astimezone(local_tz).date()

    ensemble_params = {
        "latitude": settings.weather_latitude,
        "longitude": settings.weather_longitude,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "models": ",".join(settings.weather_ensemble_models),
        "forecast_days": settings.weather_forecast_days,
        "timezone": settings.weather_timezone,
    }
    forecast_params = {
        "latitude": settings.weather_latitude,
        "longitude": settings.weather_longitude,
        "hourly": "temperature_2m",
        "temperature_unit": "fahrenheit",
        "models": _forecast_models_from_ensemble_models(settings.weather_ensemble_models),
        "forecast_days": settings.weather_forecast_days,
        "timezone": settings.weather_timezone,
    }
    client = session or requests.Session()
    payload: dict[str, object] | None = None
    endpoint_attempts = [
        ("https://api.open-meteo.com/v1/ensemble", ensemble_params),
        ("https://api.open-meteo.com/v1/forecast", forecast_params),
    ]
    for endpoint, params in endpoint_attempts:
        try:
            response = client.get(endpoint, params=params, timeout=20)
            response.raise_for_status()
            candidate = response.json()
            if isinstance(candidate, dict):
                payload = candidate
                break
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.warning("open_meteo_request_failed endpoint=%s status=%s", endpoint, status)
        except requests.RequestException:
            logger.warning("open_meteo_request_failed endpoint=%s", endpoint, exc_info=True)
    if payload is None:
        # Degrade gracefully; pipeline can still run Kalshi + crypto collectors.
        return []

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
