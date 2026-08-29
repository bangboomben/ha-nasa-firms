"""Constants for the NASA FIRMS integration."""
from datetime import timedelta

DOMAIN = "nasa_firms"

CONF_MAP_KEY = "map_key"
CONF_REGION = "region"
CONF_SATELLITES = "satellites"
CONF_WINDOW = "window"
CONF_MIN_CONFIDENCE = "min_confidence"
CONF_MIN_FRP = "min_frp"
CONF_IGNORE_ZONES = "ignore_zones"
CONF_WIND_FIRES = "wind_fires"
CONF_AUTO_IGNORE = "auto_ignore"

# A zone is only ever meant to cover a known heat source and its immediate
# surroundings — a plant, a flare stack, a landfill. Wide enough and it starts
# hiding real fires next door, which is the one thing this feature must not do.
DEFAULT_ZONE_RADIUS_M = 1_000
MAX_ZONE_RADIUS_M = 20_000

DEFAULT_SATELLITES = ["noaa20", "noaa21", "snpp"]
DEFAULT_RADIUS_M = 100_000
# The location selector happily lets a user drag the circle across a continent,
# and past a certain size FETCH_COUNT silently clips the result. 500 km is five
# times the default and still answers "my area and well beyond it"; past that
# the question has stopped being "what is near me". Enforced on new entries
# only — an existing entry configured wider keeps working and reports the
# truncation instead.
MAX_RADIUS_M = 500_000
DEFAULT_MIN_CONFIDENCE = "any"
DEFAULT_MIN_FRP = 0.0

# Off by default, deliberately. The detection itself is safe — calibrated
# against 39,397 real detections across seven regions without suppressing a
# single one at or above 50 MW — but it needs 60 days of history before it
# does anything at all. A filter that stays dormant for two months and then
# starts hiding fire data on its own is the wrong thing to hand someone who
# never asked for it, and it matches the ignore zones, which also start empty.
DEFAULT_AUTO_IGNORE = False

# How long the per-cell history is kept in the store, and how the file is
# named. One store per entry: two entries watch different areas, and a shared
# file would make removing one of them clear the other's history.
SOURCES_STORAGE_VERSION = 1
SOURCES_STORAGE_KEY = f"{DOMAIN}.sources"

# How many of the nearest fires get a wind reading each cycle. Configurable
# because it changes the number of met.no requests, exactly like the satellite
# choice changes FIRMS calls — the project's own bar for what earns an option.
# The cap is budget arithmetic, not a technical limit: at 5 fires a cycle,
# a thousand installations stay comfortably inside met.no's stated request
# threshold under this integration's shared User-Agent. Fires ranked beyond
# the limit simply carry no reading.
DEFAULT_WIND_FIRES = 3
MAX_WIND_FIRES = 5

# NASA refreshes FIRMS NRT data roughly every 15 minutes — polling faster
# only burns request quota without new data.
UPDATE_INTERVAL = timedelta(minutes=15)

# VIIRS pixel is 375 m; 1 km absorbs cross-satellite geolocation jitter.
CLUSTER_RADIUS_KM = 1.0
FETCH_COUNT = 1000

GEO_SOURCE = "nasa_firms"
ATTRIBUTION = "Data courtesy of NASA FIRMS"
# met.no publishes under CC BY 4.0, which requires the credit and the licence
# link. Only shown while wind data is actually present.
ATTRIBUTION_WEATHER = (
    "Wind data from MET Norway (CC BY 4.0, creativecommons.org/licenses/by/4.0/)"
)
# GeoNames ships under the same licence and the same rule applies: credit it
# only while a place name is actually on the entity.
ATTRIBUTION_PLACES = (
    "Place names from GeoNames (CC BY 4.0, creativecommons.org/licenses/by/4.0/)"
)

# Where the shared place index lives in hass.data. One per Home Assistant, not
# per entry: it is ~6 MB of place names (measured, 170,607 places) and two
# entries want the same table.
DATA_PLACES = "places"

# One repair notice for the whole integration, not one per entry. The
# CelesTrak client and its cooldown are shared across every config entry
# (see _async_orbit_client), so an orbit outage is never about one location
# -- v0.9.0 raised the same warning once per entry, which said the same thing
# twice to anyone watching two areas and five times to anyone watching five.
ORBIT_ISSUE_ID = "orbit_http"

# met.no hard-blocks generic agents: their ToS wants the application and a
# contact address. The version comes from the manifest so there is only ever
# one place to bump.
USER_AGENT = "ha-nasa-firms/{version} github.com/bangboomben/ha-nasa-firms"

MAP_KEY_URL = "https://firms.modaps.eosdis.nasa.gov/api/map_key/"
