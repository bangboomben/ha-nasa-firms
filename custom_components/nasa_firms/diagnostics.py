"""Diagnostics dump: everything needed to answer a bug report, and nothing else.

Home Assistant wraps this with its own version, the integration version and the
manifest, so none of that is repeated here.

**What is deliberately left out.** The MAP_KEY, obviously. But also the
coordinates: the monitored location is someone's house for most users, an
ignore zone is the factory next to it, and a diagnostics dump exists to be
pasted into a public issue. What a bug report actually needs is the *shape* of
the setup — which regional service, how large an area, how many fires, how far
away — and that survives redaction intact. Distances and bearings are relative
to an origin that is not in the file.
"""
from __future__ import annotations

from typing import Any

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.const import CONF_LATITUDE, CONF_LONGITUDE
from homeassistant.core import HomeAssistant

from .api import SOURCE_MIN_SPAN_DAYS
from .const import CONF_IGNORE_ZONES, CONF_MAP_KEY, CONF_REGION, FETCH_COUNT
from .coordinator import NasaFirmsConfigEntry

# `async_redact_data` walks nested dicts and lists, so the coordinates inside
# each ignore zone are covered by the same two keys as the entry's own.
TO_REDACT = {CONF_MAP_KEY, CONF_LATITUDE, CONF_LONGITUDE, "location"}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant, entry: NasaFirmsConfigEntry
) -> dict[str, Any]:
    """Return diagnostics for a config entry."""
    coordinator = entry.runtime_data
    data = coordinator.data
    zones = entry.options.get(CONF_IGNORE_ZONES) or []

    return {
        "entry": {
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
        },
        "setup": {
            # The region is derived from the coordinates, so it is the one
            # place a location problem shows up without printing the location.
            "region": entry.data.get(CONF_REGION),
            "radius_km": coordinator.radius_km,
            "satellites": coordinator.satellites,
            "window": coordinator.window,
            "min_confidence": coordinator.min_confidence,
            "min_frp": coordinator.min_frp,
            "ignore_zones": len(zones),
            "ignore_zone_radii_m": [z.get("radius") for z in zones],
            "auto_ignore": coordinator.auto_ignore,
        },
        # The automatic ignores decide on their own, so "why is that fire not
        # showing up" has to be answerable from here. No cell keys: a key is
        # the coordinates, which is the one thing this file does not print.
        # Span and baseline are enough to tell a recognised plant from a cell
        # that is merely on its way there.
        "learned_sources": {
            "tracked_cells": coordinator.sources.tracked_cells,
            "known_sources": len(coordinator.sources.known_sources),
            "span_threshold_days": SOURCE_MIN_SPAN_DAYS,
            "sources": [
                {
                    "active_days": cell.active_days,
                    "span_days": cell.span_days,
                    "baseline_frp_mw": round(cell.baseline_frp, 1),
                }
                for cell in coordinator.sources.known_sources
            ],
        },
        "last_cycle": {
            "success": coordinator.last_update_success,
            "raw_detections": data.raw_detections,
            "per_satellite": data.per_satellite,
            "satellite_errors": data.satellite_errors,
            # True means every count below is too low. It is the first thing to
            # check when someone reports "it is not finding fires I can see".
            "truncated": data.truncated,
            "fetch_cap": FETCH_COUNT,
            "ignored_detections": data.ignored_detections,
            "auto_ignored_detections": data.auto_ignored_detections,
            "fires": len(data.clusters),
            "nearest_km": data.nearest_km,
            "max_frp": data.max_frp,
        },
        "weather": {
            # readings < wind_fires with failing False is normal — fires
            # inside one rounded coordinate share a reading, and a parse miss
            # is silent by design. readings 0 with failing True is an outage.
            "wind_fires": coordinator.wind_fires,
            "readings": len(data.wind),
            "failing": coordinator.weather_failing,
            "nearest_bearing": data.nearest_wind.bearing if data.nearest_wind else None,
            "nearest_speed_ms": data.nearest_wind.speed if data.nearest_wind else None,
            "nearest_forecast_step": (
                data.nearest_wind.time if data.nearest_wind else None
            ),
        },
        # The place *names* are deliberately absent. "Montpellier" identifies
        # the monitored area as surely as the coordinates this file redacts,
        # and the questions a bug report raises — did the dataset load, did it
        # resolve anything — are answered by the shape alone.
        "orbits": {
            "failing": coordinator.orbits_failing,
        },
        "places": {
            "dataset_loaded": coordinator.places.loaded,
            "dataset_size": len(coordinator.places),
            "resolved": len(data.places),
            # True means the bundled file is missing or corrupt: a broken
            # install rather than an outage, since nothing here is fetched.
            "failing": coordinator.places_failing,
        },
        # No ids and no coordinates: a cluster id is its rounded position.
        "fires": [
            {
                "distance_km": c.distance_km,
                "bearing": c.bearing,
                "frp_mw": c.frp,
                "confidence": c.confidence,
                "brightness_k": c.brightness,
                "satellites": c.satellites,
                "detections": c.detections,
                "acquired": c.acq_datetime,
                # Membership only — the id itself would be the coordinates.
                "has_wind": c.id in data.wind,
            }
            for c in data.clusters
        ],
    }
