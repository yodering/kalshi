from __future__ import annotations

from datetime import date, datetime, time, timedelta, timezone
import logging
import re
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


def _model_from_member_key(member_key: str, fallback: str) -> str:
    key = member_key.lower()
    if "ecmwf" in key:
        return "ecmwf_ifs025_ensemble"
    if "gfs" in key:
        return "gfs_ensemble"
    if "icon" in key:
        return "icon_seamless"
    if "hrrr" in key:
        return "hrrr_conus"
    if "best_match" in key:
        return "best_match"
    return fallback


def _parse_member_index(member_key: str) -> int | None:
    match = re.search(r"member[_-]?(\d+)", member_key, flags=re.IGNORECASE)
    if not match:
        return None
    return int(match.group(1))


def _parse_local_time(time_value: str, tz_name: str) -> datetime | None:
    candidate = time_value.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(candidate)
    except ValueError:
        return None
    tz = ZoneInfo(tz_name)
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=tz)
    return parsed.astimezone(tz)


def _is_dst(target_date: date, tz_name: str) -> bool:
    tz = ZoneInfo(tz_name)
    probe = datetime.combine(target_date, time(hour=12, minute=0), tzinfo=tz)
    return bool(probe.dst())


def _measurement_window(target_date: date, tz_name: str) -> tuple[datetime, datetime]:
    tz = ZoneInfo(tz_name)
    if _is_dst(target_date, tz_name):
        start = datetime.combine(target_date, time(hour=1, minute=0), tzinfo=tz)
        end = start + timedelta(days=1)
        return start, end
    start = datetime.combine(target_date, time(hour=0, minute=0), tzinfo=tz)
    end = start + timedelta(days=1)
    return start, end


def _extract_daily_max(
    *, hourly_values: list[object], hourly_times: list[object], target_date: date, tz_name: str
) -> float | None:
    start, end = _measurement_window(target_date, tz_name)
    max_temp: float | None = None
    for raw_temp, raw_time in zip(hourly_values, hourly_times):
        if not isinstance(raw_time, str):
            continue
        local_dt = _parse_local_time(raw_time, tz_name)
        if local_dt is None:
            continue
        if not (start <= local_dt < end):
            continue
        temp = _as_float(raw_temp)
        if temp is None:
            continue
        if max_temp is None or temp > max_temp:
            max_temp = temp
    return max_temp


def _forecast_models_from_ensemble_models(models: list[str]) -> str:
    mapped: list[str] = ["best_match", "hrrr_conus"]
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
    deduped = list(dict.fromkeys(mapped))
    return ",".join(deduped)


def _extract_samples_from_payload(
    *,
    payload: dict[str, object],
    target_date: date,
    tz_name: str,
    collected_at: datetime,
    source: str,
    fallback_model: str,
) -> list[WeatherEnsembleSample]:
    hourly = payload.get("hourly")
    if not isinstance(hourly, dict):
        return []
    times = hourly.get("time")
    if not isinstance(times, list) or not times:
        return []

    member_keys = [
        key
        for key, values in hourly.items()
        if key != "time" and key.lower().startswith("temperature_2m") and isinstance(values, list)
    ]
    samples: list[WeatherEnsembleSample] = []
    for member_key in member_keys:
        values = hourly.get(member_key)
        if not isinstance(values, list):
            continue
        if len(values) != len(times):
            continue
        day_max = _extract_daily_max(
            hourly_values=values,
            hourly_times=times,
            target_date=target_date,
            tz_name=tz_name,
        )
        if day_max is None:
            continue
        member_index = _parse_member_index(member_key)
        member = f"member{member_index:02d}" if member_index is not None else member_key
        samples.append(
            WeatherEnsembleSample(
                collected_at=collected_at,
                target_date=target_date,
                model=_model_from_member_key(member_key, fallback_model),
                member=member,
                max_temp_f=day_max,
                source=source,
                raw_json={"member_key": member_key},
            )
        )
    return samples


def fetch_weather_ensemble_samples(
    settings: Settings,
    *,
    session: requests.Session | None = None,
    now_utc: datetime | None = None,
) -> list[WeatherEnsembleSample]:
    current_utc = now_utc or datetime.now(timezone.utc)
    local_tz = ZoneInfo(settings.weather_timezone)
    target_date = current_utc.astimezone(local_tz).date()
    client = session or requests.Session()

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

    samples: list[WeatherEnsembleSample] = []

    # Primary attempts: ensemble endpoints (Open-Meteo has changed hosts over time).
    ensemble_endpoints = [
        "https://ensemble-api.open-meteo.com/v1/ensemble",
        "https://api.open-meteo.com/v1/ensemble",
    ]
    for endpoint in ensemble_endpoints:
        try:
            ensemble_response = client.get(
                endpoint,
                params=ensemble_params,
                timeout=20,
            )
            ensemble_response.raise_for_status()
            ensemble_payload = ensemble_response.json()
            if isinstance(ensemble_payload, dict):
                extracted = _extract_samples_from_payload(
                    payload=ensemble_payload,
                    target_date=target_date,
                    tz_name=settings.weather_timezone,
                    collected_at=current_utc,
                    source="open-meteo-ensemble",
                    fallback_model="ensemble",
                )
                if extracted:
                    samples.extend(extracted)
                    break
        except requests.HTTPError as exc:
            status = exc.response.status_code if exc.response is not None else "unknown"
            logger.warning("open_meteo_request_failed endpoint=%s status=%s", endpoint, status)
        except requests.RequestException:
            logger.warning("open_meteo_request_failed endpoint=%s", endpoint, exc_info=True)

    # Fallback and cross-reference: deterministic forecast endpoint.
    try:
        forecast_response = client.get(
            "https://api.open-meteo.com/v1/forecast",
            params=forecast_params,
            timeout=20,
        )
        forecast_response.raise_for_status()
        forecast_payload = forecast_response.json()
        if isinstance(forecast_payload, dict):
            deterministic_samples = _extract_samples_from_payload(
                payload=forecast_payload,
                target_date=target_date,
                tz_name=settings.weather_timezone,
                collected_at=current_utc,
                source="open-meteo-forecast",
                fallback_model="best_match",
            )
            if samples:
                # Keep deterministic only as cross-reference if we already have ensemble.
                # Prefix member names to avoid collisions in unique constraint.
                cross_reference: list[WeatherEnsembleSample] = []
                for sample in deterministic_samples:
                    cross_reference.append(
                        WeatherEnsembleSample(
                            collected_at=sample.collected_at,
                            target_date=sample.target_date,
                            model=sample.model,
                            member=f"det_{sample.member}",
                            max_temp_f=sample.max_temp_f,
                            source=sample.source,
                            raw_json=sample.raw_json,
                        )
                    )
                samples.extend(cross_reference)
            else:
                # Hard fallback if ensemble endpoint is unavailable.
                samples.extend(deterministic_samples)
    except requests.HTTPError as exc:
        status = exc.response.status_code if exc.response is not None else "unknown"
        logger.warning(
            "open_meteo_request_failed endpoint=%s status=%s",
            "https://api.open-meteo.com/v1/forecast",
            status,
        )
    except requests.RequestException:
        logger.warning(
            "open_meteo_request_failed endpoint=%s",
            "https://api.open-meteo.com/v1/forecast",
            exc_info=True,
        )

    return samples
