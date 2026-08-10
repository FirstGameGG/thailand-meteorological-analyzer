from __future__ import annotations

import io
import json
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Dict

import meteostat as ms
import pandas as pd
import requests
import streamlit as st
import urllib3

REQUEST_TIMEOUT_SECONDS = 30
MAX_RETRIES = 3
BASE_BACKOFF_SECONDS = 1.0
MAX_PARALLEL_WORKERS = 12
ONI_SOURCE_URL = "https://www.cpc.ncep.noaa.gov/products/analysis_monitoring/enso/oni/v6/"

# Anchor the station master file to this module's directory so the app works
# regardless of the current working directory it is launched from.
STATIONS_FILE = Path(__file__).resolve().parent / "stations.json"

# Bank of Thailand-harmonized categorical palette. Validated with the dataviz
# skill (Machado-2009 CVD, --pairs all on a white surface): worst pair ΔE 12.3,
# above the ≥12 target. Colour is a secondary cue here — the map/legend/hover
# also carry the region name.
REGION_COLORS = {
    "ภาคเหนือ": "#1f7fd6",              # blue-500
    "ภาคตะวันออกเฉียงเหนือ": "#a97c1f",  # gold-600
    "ภาคตะวันตก": "#2a7d4f",            # green-600
    "ภาคกลาง": "#c0342a",              # red-600
    "ภาคตะวันออก": "#4a3aa7",           # violet
    "ภาคใต้": "#1a9e8f",               # teal
}


@st.cache_data(show_spinner=False)
def load_stations() -> Dict[str, Dict[str, Any]]:
    if not STATIONS_FILE.exists():
        raise FileNotFoundError("ไม่พบไฟล์ stations.json กรุณาแปลงไฟล์สถานีเป็น JSON ก่อนใช้งาน")

    with open(STATIONS_FILE, "r", encoding="utf-8") as f:
        stations = json.load(f)

    if not isinstance(stations, dict):
        raise ValueError("โครงสร้างไฟล์ stations.json ไม่ถูกต้อง")

    cleaned_stations: Dict[str, Dict[str, Any]] = {}
    for station_id, payload in stations.items():
        if not isinstance(payload, dict):
            continue

        required_keys = {"name", "address", "lat", "lon", "region"}
        if not required_keys.issubset(payload.keys()):
            continue

        try:
            cleaned_stations[str(station_id)] = {
                "name": str(payload["name"]).strip(),
                "address": str(payload["address"]).strip(),
                "lat": float(payload["lat"]),
                "lon": float(payload["lon"]),
                "region": str(payload["region"]).strip(),
            }
        except (TypeError, ValueError):
            continue

    if not cleaned_stations:
        raise ValueError("ไฟล์ stations.json ไม่มีข้อมูลสถานีที่ใช้งานได้")

    return cleaned_stations


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_daily_dataframe(station_id: str, start, end):
    if hasattr(ms, "Daily"):
        return ms.Daily(station_id, start, end).fetch()

    if hasattr(ms, "daily"):
        ts = ms.daily(station_id, start, end)
        if hasattr(ts, "fetch"):
            return ts.fetch()
        return ts

    raise AttributeError("Meteostat API ไม่รองรับ Daily/daily ในสภาพแวดล้อมนี้")


@st.cache_data(ttl=86400, show_spinner=False)
def find_nearest_station_id(lat: float, lon: float):
    if hasattr(ms, "Stations"):
        nearby = ms.Stations().nearby(lat, lon).fetch(1)
        if not nearby.empty:
            return nearby.index[0]
        return None

    if hasattr(ms, "stations"):
        if not hasattr(ms, "Point"):
            raise AttributeError("Meteostat API ไม่มี Point สำหรับค้นหาสถานีใกล้เคียง")

        point = ms.Point(lat, lon)
        nearby = ms.stations.nearby(point, limit=1)

        if hasattr(nearby, "fetch"):
            nearby = nearby.fetch(1)

        if nearby is not None and not nearby.empty:
            return nearby.index[0]
        return None

    raise AttributeError("Meteostat API ไม่รองรับ Stations/stations ในสภาพแวดล้อมนี้")


@st.cache_data(ttl=86400, show_spinner=False)
def fetch_oni_data() -> pd.DataFrame:
    urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

    response = requests.get(ONI_SOURCE_URL, verify=False, timeout=REQUEST_TIMEOUT_SECONDS)
    response.raise_for_status()

    dfs = pd.read_html(io.StringIO(response.text))
    if not dfs:
        raise ValueError("ไม่พบตารางข้อมูล ONI จากแหล่งข้อมูล NOAA")

    df_oni_raw = None
    for table in dfs:
        columns = [str(c).strip() for c in table.columns]
        if "Year" in columns or (not table.empty and str(table.iloc[0, 0]).strip() == "Year"):
            df_oni_raw = table.copy()
            break

    if df_oni_raw is None:
        raise ValueError("ไม่พบตาราง ONI ที่มีคอลัมน์ Year")

    if str(df_oni_raw.iloc[0, 0]).strip() == "Year":
        df_oni_raw.columns = df_oni_raw.iloc[0]
        df_oni_raw = df_oni_raw[1:]

    df_oni_raw = df_oni_raw[df_oni_raw["Year"] != "Year"].copy()

    season_to_month = {
        "DJF": 1,
        "JFM": 2,
        "FMA": 3,
        "MAM": 4,
        "AMJ": 5,
        "MJJ": 6,
        "JJA": 7,
        "JAS": 8,
        "ASO": 9,
        "SON": 10,
        "OND": 11,
        "NDJ": 12,
    }

    df_oni = df_oni_raw.melt(id_vars=["Year"], var_name="Season", value_name="ONI_Index")
    df_oni["Month"] = df_oni["Season"].map(season_to_month)
    df_oni = df_oni.dropna(subset=["Month", "ONI_Index"])
    df_oni = df_oni[df_oni["ONI_Index"] != ""]
    df_oni["ONI_Index"] = pd.to_numeric(df_oni["ONI_Index"], errors="coerce")
    df_oni["year_month"] = df_oni.apply(lambda row: f"{int(row['Year'])}-{int(row['Month']):02d}", axis=1)

    result = df_oni[["year_month", "ONI_Index"]].sort_values("year_month").reset_index(drop=True)
    if result.empty:
        raise ValueError("ตาราง ONI ว่างหลังจากทำความสะอาดข้อมูล")
    return result


def fetch_station_with_retry(wmo_id: str, info: Dict[str, Any], start_date, query_end_date) -> Dict[str, Any]:
    station_df = pd.DataFrame()
    station_source = "wmo"
    station_reason = ""
    station_attempt_logs = []

    for attempt in range(1, MAX_RETRIES + 1):
        try:
            station_df = fetch_daily_dataframe(wmo_id, start_date, query_end_date)
            if station_df is not None and not station_df.empty:
                station_reason = "success"
                break
            station_reason = "empty"
            station_attempt_logs.append(f"WMO attempt {attempt}: empty")
        except Exception as e:
            station_reason = "exception"
            station_attempt_logs.append(f"WMO attempt {attempt}: {type(e).__name__}: {str(e)}")

        if attempt < MAX_RETRIES:
            time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))

    if station_df is None or station_df.empty:
        try:
            fallback_station_id = find_nearest_station_id(info["lat"], info["lon"])
            if fallback_station_id is not None:
                for attempt in range(1, MAX_RETRIES + 1):
                    try:
                        station_df = fetch_daily_dataframe(fallback_station_id, start_date, query_end_date)
                        if station_df is not None and not station_df.empty:
                            station_source = "fallback"
                            station_reason = "success"
                            station_attempt_logs.append(
                                f"Fallback station {fallback_station_id} success on attempt {attempt}"
                            )
                            break
                        station_reason = "empty"
                        station_attempt_logs.append(
                            f"Fallback station {fallback_station_id} attempt {attempt}: empty"
                        )
                    except Exception as e:
                        station_reason = "exception"
                        station_attempt_logs.append(
                            f"Fallback station {fallback_station_id} attempt {attempt}: {type(e).__name__}: {str(e)}"
                        )

                    if attempt < MAX_RETRIES:
                        time.sleep(BASE_BACKOFF_SECONDS * (2 ** (attempt - 1)))
            else:
                station_reason = "empty"
                station_attempt_logs.append("Fallback lookup: no nearby station returned by Meteostat")
        except Exception as e:
            station_reason = "exception"
            station_attempt_logs.append(f"Fallback lookup failed: {type(e).__name__}: {str(e)}")

    if station_df is not None and not station_df.empty:
        station_df = station_df.copy()
        station_df["wmo_id"] = wmo_id
        station_df["source"] = station_source

        thai_source = "สถานีหลัก" if station_source == "wmo" else "สถานีใกล้เคียง (Fallback)"
        thai_detail = (
            "ดึงข้อมูลจากสถานีหลักสำเร็จ"
            if station_source == "wmo"
            else "สถานีหลักไม่มีข้อมูลหรือขัดข้อง จึงดึงทดแทนจากสถานีใกล้เคียง"
        )

        return {
            "ok": True,
            "weather_df": station_df,
            "fetched_station": {
                "ชื่อสถานี": info["name"],
                "ที่อยู่": info["address"],
                "ภูมิภาค": info["region"],
                "ละติจูด": info["lat"],
                "ลองจิจูด": info["lon"],
                "รหัสสถานี (WMO)": wmo_id,
                "แหล่งข้อมูล": thai_source,
                "source": station_source,
            },
            "station_result": {
                "รหัสสถานี": wmo_id,
                "ชื่อสถานี": info["name"],
                "สถานะ": "สำเร็จ",
                "แหล่งที่มา": thai_source,
                "รายละเอียดเพิ่มเติม": thai_detail,
            },
        }

    detail = "; ".join(station_attempt_logs[-3:]) if station_attempt_logs else "no detail"
    lower_detail = detail.lower()
    if "timeout" in lower_detail:
        final_reason = "timeout"
        thai_detail = "เชื่อมต่อเกินเวลาที่กำหนด (Timeout)"
    elif station_reason == "empty":
        final_reason = "empty"
        thai_detail = "ช่วงเวลาดังกล่าวไม่มีข้อมูลในฐานข้อมูล"
    else:
        final_reason = "exception"
        thai_detail = "เกิดความผิดพลาดในการเชื่อมต่อ API"

    return {
        "ok": False,
        "failed_station": {
            "wmo_id": wmo_id,
            "name": info["name"],
            "reason": final_reason,
            "detail": detail,
        },
        "station_result": {
            "รหัสสถานี": wmo_id,
            "ชื่อสถานี": info["name"],
            "สถานะ": "ล้มเหลว",
            "แหล่งที่มา": "-",
            "รายละเอียดเพิ่มเติม": thai_detail,
        },
    }


def fetch_stations_parallel(wmo_stations, start_date, query_end_date, progress_bar, status_text):
    all_weather_data = []
    fetched_stations = []
    failed_stations = []
    station_results = []

    station_items = list(wmo_stations.items())
    total_stations = len(station_items)
    max_workers = min(MAX_PARALLEL_WORKERS, total_stations) if total_stations > 0 else 1

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        future_to_station = {
            executor.submit(fetch_station_with_retry, wmo_id, info, start_date, query_end_date): (wmo_id, info)
            for wmo_id, info in station_items
        }

        completed = 0
        for future in as_completed(future_to_station):
            wmo_id, info = future_to_station[future]
            completed += 1
            status_text.text(f"กำลังดำเนินการสืบค้นข้อมูลสถานี: {info['name']} ({completed}/{total_stations})")

            try:
                result = future.result()
            except Exception as e:
                result = {
                    "ok": False,
                    "failed_station": {
                        "wmo_id": wmo_id,
                        "name": info["name"],
                        "reason": "exception",
                        "detail": f"Unhandled error: {type(e).__name__}: {str(e)}",
                    },
                    "station_result": {
                        "รหัสสถานี": wmo_id,
                        "ชื่อสถานี": info["name"],
                        "สถานะ": "ล้มเหลว",
                        "แหล่งที่มา": "-",
                        "รายละเอียดเพิ่มเติม": "เกิดความผิดพลาดในการเชื่อมต่อ API",
                    },
                }

            if result["ok"]:
                all_weather_data.append(result["weather_df"])
                fetched_stations.append(result["fetched_station"])
                station_results.append(result["station_result"])
            else:
                failed_stations.append(result["failed_station"])
                station_results.append(result["station_result"])

            progress_bar.progress(completed / total_stations)

    return all_weather_data, fetched_stations, failed_stations, station_results
