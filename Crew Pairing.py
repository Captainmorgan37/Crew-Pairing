import math
from math import atan2, cos, radians, sin, sqrt

import pandas as pd
import streamlit as st
import xml.etree.ElementTree as ET


# Approximate latitude/longitude for crew bases used to prioritise nearby pairings.
# Values are expressed as (latitude, longitude) in decimal degrees.
BASE_COORDINATES = {
    "CYYC": (51.1139, -114.0203),  # Calgary
    "CYLW": (49.9561, -119.3770),  # Kelowna
    "CYVR": (49.1939, -123.1833),  # Vancouver
    "CYEG": (53.3097, -113.5797),  # Edmonton
    "CYXE": (52.1708, -106.7000),  # Saskatoon
    "CYWG": (49.9100, -97.2399),   # Winnipeg
    "CYYZ": (43.6777, -79.6248),   # Toronto Pearson
    "CYUL": (45.4706, -73.7408),   # Montréal
    "CYOW": (45.3225, -75.6692),   # Ottawa
    "CYXU": (43.0356, -81.1539),   # London (Ontario)
}


def haversine_distance_km(coord_a, coord_b):
    """Return the great-circle distance in kilometres between two coordinates."""

    lat1, lon1 = coord_a
    lat2, lon2 = coord_b

    rlat1, rlon1 = radians(lat1), radians(lon1)
    rlat2, rlon2 = radians(lat2), radians(lon2)

    dlat = rlat2 - rlat1
    dlon = rlon2 - rlon1

    a = sin(dlat / 2) ** 2 + cos(rlat1) * cos(rlat2) * sin(dlon / 2) ** 2
    c = 2 * atan2(sqrt(a), sqrt(1 - a))
    earth_radius_km = 6371.0
    return earth_radius_km * c


def compute_base_distance(base_a, base_b, missing_registry):
    """Distance in kilometres between two bases, tracking any that are unknown."""

    if pd.isna(base_a) or pd.isna(base_b):
        return math.inf

    base_a = str(base_a).strip().upper()
    base_b = str(base_b).strip().upper()
    if base_a == base_b:
        return 0.0

    coord_a = BASE_COORDINATES.get(base_a)
    coord_b = BASE_COORDINATES.get(base_b)

    if coord_a and coord_b:
        return haversine_distance_km(coord_a, coord_b)

    if not coord_a:
        missing_registry.add(base_a)
    if not coord_b:
        missing_registry.add(base_b)
    return math.inf

st.set_page_config(page_title="Crew Pairing Optimizer", layout="wide")
st.title("🧩 Crew Pairing Optimizer")

st.write("Upload your QUAL.xml and ACTS file to visualize all possible PIC/SIC pairings by shared availability.")

# -------------------------------
# File uploaders
# -------------------------------
qual_file = st.file_uploader("Upload QUAL.xml", type=["xml"])
acts_file = st.file_uploader("Upload ACTS file")

if qual_file and acts_file:
    # -------------------------------
    # Parse QUAL.XML
    # -------------------------------
    ns = {"ns": "http://www.ad-opt.com/2009/Altitude/data"}
    tree = ET.parse(qual_file)
    root = tree.getroot()

    pilots = []
    for emp in root.findall("ns:employee", ns):
        emp_id = emp.findtext("ns:employee-id", namespaces=ns)
        seat = emp.findtext("ns:primary-seat-qual", namespaces=ns)
        name = emp.findtext("ns:name", namespaces=ns)
        base_elem = emp.find("ns:base", ns)
        base = base_elem.get("ref") if base_elem is not None else None
        ac_elem = emp.find(".//ns:aircraft", ns)
        aircraft = ac_elem.get("ref") if ac_elem is not None else None
        pilots.append({
            "employee_id": emp_id,
            "seat": seat,
            "name": name,
            "base": base,
            "aircraft": aircraft,
        })

    df_qual = pd.DataFrame(pilots)

    # -------------------------------
    # Parse ACTS file
    # -------------------------------
    acts_data = []
    text = acts_file.read().decode("utf-8")
    for line in text.splitlines():
        parts = line.split()
        if len(parts) < 8:
            continue
        emp_id = parts[0]
        code = parts[3]
        base = parts[4]
        date = parts[7]
        if code in ["A", "DRAFT"]:
            duty = "Available"
        elif code in ["OFF", "H", "Z"]:
            duty = "Off"
        else:
            continue
        acts_data.append({"employee_id": emp_id, "date": date, "duty": duty, "base": base})

    df_acts = pd.DataFrame(acts_data)

    # -------------------------------
    # Merge and process
    # -------------------------------
    merged = df_acts.merge(
        df_qual,
        on="employee_id",
        how="left",
        suffixes=("_acts", "_qual"),
    )

    merged["base"] = merged["base_qual"].combine_first(merged["base_acts"])
    merged = merged.drop(columns=["base_acts", "base_qual"])

    # Filter available-only records for overlap computation
    available = merged[merged["duty"] == "Available"]

    # Separate PIC/SIC
    pic_df = available[available["seat"] == "PIC"]
    sic_df = available[available["seat"] == "SIC"]

    pic_availability = pic_df.groupby("employee_id")["date"].agg(set)
    sic_availability = sic_df.groupby("employee_id")["date"].agg(set)

    # -------------------------------
    # Compute pair overlaps prioritising nearby bases
    # -------------------------------
    pair_results = []
    missing_base_codes = set()

    for ac, pic_group in pic_df.groupby("aircraft"):
        sics = sic_df[sic_df["aircraft"] == ac]
        if sics.empty:
            continue

        for _, pic_row in pic_group.iterrows():
            pic_base = pic_row.get("base")
            pic_dates = pic_availability.get(pic_row.employee_id, set())
            if not pic_dates:
                continue

            sics_sorted = sics.assign(
                base_distance=sics["base"].apply(
                    lambda sic_base: compute_base_distance(pic_base, sic_base, missing_base_codes)
                )
            ).sort_values(by=["base_distance", "employee_id"], ascending=[True, True], kind="mergesort")

            for _, sic_row in sics_sorted.iterrows():
                sic_dates = sic_availability.get(sic_row.employee_id, set())
                overlap_days = len(pic_dates.intersection(sic_dates))
                if overlap_days == 0:
                    continue

                distance_km = sic_row["base_distance"]
                distance_value = round(distance_km, 1) if math.isfinite(distance_km) else float("nan")

                pic_identifier = pic_row.get("name")
                if pd.isna(pic_identifier) or pic_identifier == "":
                    pic_identifier = pic_row.get("employee_id")

                sic_identifier = sic_row.get("name")
                if pd.isna(sic_identifier) or sic_identifier == "":
                    sic_identifier = sic_row.get("employee_id")

                pair_results.append({
                    "PIC": pic_identifier,
                    "SIC": sic_identifier,
                    "PIC Base": pic_base,
                    "SIC Base": sic_row.get("base"),
                    "Aircraft": ac,
                    "Base Distance (km)": distance_value,
                    "Overlap Days": overlap_days,
                })

    df_pairs = pd.DataFrame(pair_results)
    if not df_pairs.empty:
        df_pairs = df_pairs.sort_values(
            by=["Base Distance (km)", "Overlap Days"],
            ascending=[True, False],
            na_position="last",
        )

    if missing_base_codes:
        missing_list = ", ".join(sorted(missing_base_codes))
        st.info(
            "No coordinates were configured for the following bases: "
            f"{missing_list}. Add them to BASE_COORDINATES to improve distance prioritisation."
        )

    st.success(f"Parsed {len(df_qual)} pilots and {len(df_acts)} duty entries.")
    st.write("### Top Pairings by Overlap")
    st.dataframe(df_pairs, use_container_width=True)

    csv = df_pairs.to_csv(index=False).encode('utf-8')
    st.download_button("Download Pairing Results (CSV)", csv, "pairings.csv", "text/csv")
