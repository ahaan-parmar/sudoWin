"""Weather (METAR) and NOTAM lookups. Never raise: every failure returns a labelled fallback.

Set GNSSWATCH_OFFLINE=1 to skip the network entirely (demo / tests).
NOTAMs are a clearly labelled SAMPLE unless FAA_CLIENT_ID and FAA_CLIENT_SECRET are set.
"""
from __future__ import annotations

import os
import re

import httpx

METAR_URL = "https://aviationweather.gov/api/data/metar"
FAA_NOTAM_URL = "https://external-api.faa.gov/notamapi/v1/notams"
GNSS_WORDS = re.compile(r"\b(GNSS|GPS|RNAV|RNP|INTERFERENCE|JAMMING|SPOOFING)\b", re.I)

SAMPLE_METARS = {
    "EVRA": "EVRA 191150Z 24012KT 9999 FEW030 14/08 Q1013 NOSIG",
    "EFHK": "EFHK 191150Z 22010KT CAVOK 12/06 Q1014 NOSIG",
    "EYVI": "EYVI 191150Z 25008KT 9999 SCT035 15/07 Q1012 NOSIG",
    "EETN": "EETN 191150Z 23011KT 9999 BKN025 12/07 Q1013 NOSIG",
    "VOBL": "VOBL 191130Z 27012KT 6000 SCT020 BKN080 27/20 Q1010 NOSIG",
}

_cache: dict[str, dict] = {}


def _icao(code: str) -> str | None:
    code = (code or "").strip().upper()
    return code if re.fullmatch(r"[A-Z0-9]{4}", code) else None


def _offline() -> bool:
    return os.environ.get("GNSSWATCH_OFFLINE") == "1"


def get_weather(icao: str, timeout: float = 5.0) -> dict:
    code = _icao(icao)
    if not code:
        return {"icao": icao, "error": "expected a 4-character ICAO airport code"}
    err = "offline mode"
    if not _offline():
        try:
            r = httpx.get(METAR_URL, params={"ids": code, "format": "json"}, timeout=timeout)
            r.raise_for_status()
            data = r.json() if r.text.strip() else []
            if data:
                m = data[0]
                _cache[code] = {"icao": code, "source": "aviationweather.gov", "live": True,
                                "raw": m.get("rawOb"), "observed_utc": m.get("reportTime"),
                                "flight_category": m.get("fltCat")}
                return _cache[code]
            err = "no METAR returned for this station"
        except Exception as e:  # network, HTTP or JSON error: fall back below
            err = f"{type(e).__name__}: {e}"
    if code in _cache:
        return {**_cache[code], "live": False, "note": f"cached earlier result ({err})"}
    if code in SAMPLE_METARS:
        return {"icao": code, "source": "built-in sample", "live": False, "raw": SAMPLE_METARS[code],
                "note": f"SAMPLE METAR, not current weather ({err})"}
    return {"icao": code, "live": False, "raw": None, "note": f"no METAR available ({err})"}


def _sample_notams(code: str, why: str) -> dict:
    text = (f"SAMPLE {code} GNSS SIGNAL MAY BE UNRELIABLE OR UNAVAILABLE WI 100NM OF THE AREA "
            "DUE TO INTERFERENCE. RNAV/RNP APCH MAY NOT BE AVBL.")
    return {"icao": code, "source": "built-in sample", "live": False, "is_sample": True,
            "note": f"SAMPLE NOTAM for demo only, NOT a real NOTAM ({why})",
            "notams": [{"id": "SAMPLE-1", "text": text, "gnss_related": True}]}


def get_notams(icao: str, timeout: float = 8.0) -> dict:
    code = _icao(icao)
    if not code:
        return {"icao": icao, "error": "expected a 4-character ICAO airport code"}
    cid, secret = os.environ.get("FAA_CLIENT_ID"), os.environ.get("FAA_CLIENT_SECRET")
    if not (cid and secret):
        return _sample_notams(code, "FAA_CLIENT_ID/FAA_CLIENT_SECRET not set")
    if _offline():
        return _sample_notams(code, "offline mode")
    try:
        r = httpx.get(FAA_NOTAM_URL, params={"icaoLocation": code, "pageSize": 50},
                      headers={"client_id": cid, "client_secret": secret}, timeout=timeout)
        r.raise_for_status()
        notams = []
        for item in r.json().get("items", []):
            n = item.get("properties", {}).get("coreNOTAMData", {}).get("notam", {})
            text = n.get("text", "")
            notams.append({"id": n.get("number") or n.get("id"), "text": text,
                           "gnss_related": bool(GNSS_WORDS.search(text))})
        notams.sort(key=lambda n: not n["gnss_related"])
        return {"icao": code, "source": "FAA NOTAM API", "live": True, "is_sample": False,
                "n_gnss_related": sum(n["gnss_related"] for n in notams), "notams": notams[:15]}
    except Exception as e:
        return _sample_notams(code, f"FAA API failed: {type(e).__name__}")
