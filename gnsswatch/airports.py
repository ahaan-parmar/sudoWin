"""Airport reference table and endpoint resolution. (P3)

Coordinates are the airport reference points from the OurAirports public
dataset (https://ourairports.com/data/, public domain), retrieved 2026-09-19
from davidmegginson.github.io/ourairports-data/airports.csv and rounded to
5 decimal places (~1 m). They are accurate to well within the corridor widths
this project reasons about (tens of km), but they are reference points, not
runway thresholds, and must not be used for navigation.

The table is deliberately small: the major hubs on the corridors this project
demonstrates, weighted towards regions where GNSS interference has been
publicly reported (Baltic, Black Sea, Eastern Mediterranean, Persian Gulf).
It is not a complete airport database.
"""
from __future__ import annotations

from typing import NamedTuple


class Airport(NamedTuple):
    icao: str
    name: str
    city: str
    country: str      # ISO 3166-1 alpha-2
    lat: float
    lon: float
    region: str       # informal grouping used by this project


# region labels: the four areas with publicly reported GNSS interference that
# the hot-zone seed covers, plus "europe"/"south_asia" for ordinary hubs.
AIRPORTS: dict[str, Airport] = {
    a.icao: a
    for a in [
        # --- Baltic ---------------------------------------------------------
        Airport("EFHK", "Helsinki Vantaa", "Helsinki", "FI", 60.31836, 24.96334, "baltic"),
        Airport("ESSA", "Stockholm Arlanda", "Stockholm", "SE", 59.64849, 17.92883, "baltic"),
        Airport("EETN", "Lennart Meri Tallinn", "Tallinn", "EE", 59.41325, 24.83264, "baltic"),
        Airport("EVRA", "Riga International", "Riga", "LV", 56.92075, 23.97071, "baltic"),
        Airport("EYVI", "Vilnius International", "Vilnius", "LT", 54.63410, 25.28580, "baltic"),
        Airport("EPWA", "Warsaw Chopin", "Warsaw", "PL", 52.16570, 20.96710, "baltic"),
        Airport("EKCH", "Copenhagen Kastrup", "Copenhagen", "DK", 55.61790, 12.65600, "baltic"),
        # --- Western Europe hubs --------------------------------------------
        Airport("EGLL", "London Heathrow", "London", "GB", 51.47075, -0.45991, "europe"),
        Airport("EDDF", "Frankfurt Main", "Frankfurt", "DE", 50.02671, 8.55835, "europe"),
        Airport("LFPG", "Paris Charles de Gaulle", "Paris", "FR", 49.00896, 2.55412, "europe"),
        Airport("EHAM", "Amsterdam Schiphol", "Amsterdam", "NL", 52.30860, 4.76389, "europe"),
        # --- Black Sea ------------------------------------------------------
        Airport("LTFM", "Istanbul", "Istanbul", "TR", 41.27487, 28.73214, "black_sea"),
        Airport("LBSF", "Sofia", "Sofia", "BG", 42.69636, 23.41767, "black_sea"),
        Airport("LROP", "Bucharest Henri Coanda", "Bucharest", "RO", 44.57179, 26.10328, "black_sea"),
        Airport("LTAC", "Ankara Esenboga", "Ankara", "TR", 40.12810, 32.99510, "black_sea"),
        # --- Eastern Mediterranean ------------------------------------------
        Airport("LTAI", "Antalya International", "Antalya", "TR", 36.89870, 30.80050, "east_med"),
        Airport("LGAV", "Athens Eleftherios Venizelos", "Athens", "GR", 37.93640, 23.94450, "east_med"),
        Airport("LCLK", "Larnaca International", "Larnaca", "CY", 34.87510, 33.62490, "east_med"),
        Airport("LLBG", "Ben Gurion", "Tel Aviv", "IL", 32.01140, 34.88670, "east_med"),
        Airport("OLBA", "Beirut Rafic Hariri", "Beirut", "LB", 33.81983, 35.48744, "east_med"),
        Airport("HECA", "Cairo International", "Cairo", "EG", 30.11153, 31.39669, "east_med"),
        # --- Persian Gulf ---------------------------------------------------
        Airport("OMDB", "Dubai International", "Dubai", "AE", 25.24979, 55.37099, "persian_gulf"),
        Airport("OMAA", "Zayed International", "Abu Dhabi", "AE", 24.44097, 54.64924, "persian_gulf"),
        Airport("OTHH", "Hamad International", "Doha", "QA", 25.27306, 51.60806, "persian_gulf"),
        Airport("OBBI", "Bahrain International", "Manama", "BH", 26.26730, 50.63764, "persian_gulf"),
        Airport("OKKK", "Kuwait International", "Kuwait City", "KW", 29.22449, 47.96981, "persian_gulf"),
        Airport("OERK", "King Khalid International", "Riyadh", "SA", 24.95760, 46.69880, "persian_gulf"),
        Airport("OIIE", "Imam Khomeini International", "Tehran", "IR", 35.41610, 51.15220, "persian_gulf"),
        # --- South Asia ------------------------------------------------------
        Airport("VIDP", "Indira Gandhi International", "New Delhi", "IN", 28.55563, 77.09519, "south_asia"),
        Airport("VOBL", "Kempegowda International", "Bengaluru", "IN", 13.19790, 77.70630, "south_asia"),
        Airport("VABB", "Chhatrapati Shivaji Maharaj International", "Mumbai", "IN", 19.08870, 72.86790, "south_asia"),
    ]
}

# Legacy/renamed ICAO codes still in circulation, mapped to the current code.
ALIASES: dict[str, str] = {
    "OKBK": "OKKK",   # Kuwait International was re-coded OKBK -> OKKK in 2022
    "LTBA": "LTFM",   # Istanbul traffic moved Ataturk -> Istanbul Airport in 2019
}


def get(icao: str) -> Airport | None:
    """Look up an airport by ICAO code. Case-insensitive, follows ALIASES."""
    key = (icao or "").strip().upper()
    key = ALIASES.get(key, key)
    return AIRPORTS.get(key)


def list_airports() -> list[Airport]:
    """All known airports, sorted by ICAO code."""
    return [AIRPORTS[k] for k in sorted(AIRPORTS)]


def resolve(place: str) -> tuple[float, float]:
    """Resolve an endpoint to (lat, lon).

    Accepts an ICAO code from AIRPORTS (case-insensitive, aliases followed) or
    a "lat,lon" string in degrees. Raises ValueError on anything else, so a
    typo surfaces immediately instead of silently routing somewhere wrong.
    """
    if not isinstance(place, str):
        raise ValueError(f"endpoint must be a string, got {type(place).__name__}")
    text = place.strip()
    if not text:
        raise ValueError("endpoint is empty")

    ap = get(text)
    if ap is not None:
        return (ap.lat, ap.lon)

    if "," in text:
        lat_s, _, lon_s = text.partition(",")
        try:
            lat, lon = float(lat_s), float(lon_s)
        except ValueError:
            raise ValueError(f"could not parse {place!r} as 'lat,lon'") from None
        if not -90.0 <= lat <= 90.0:
            raise ValueError(f"latitude {lat} out of range [-90, 90]")
        if not -180.0 <= lon <= 180.0:
            raise ValueError(f"longitude {lon} out of range [-180, 180]")
        return (lat, lon)

    raise ValueError(
        f"unknown endpoint {place!r}: expected an ICAO code from airports.AIRPORTS "
        f"({len(AIRPORTS)} known) or a 'lat,lon' string"
    )
