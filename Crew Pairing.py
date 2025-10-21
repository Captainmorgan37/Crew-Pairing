import streamlit as st
import pandas as pd
import xml.etree.ElementTree as ET
from io import StringIO
from itertools import product

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

    df_qual = pd.DataFrame(
        pilots,
        columns=["employee_id", "seat", "name", "base", "aircraft"],
    )

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

    df_acts = pd.DataFrame(
        acts_data,
        columns=["employee_id", "date", "duty", "base"],
    )

    if df_acts.empty:
        st.warning(
            "No usable ACTS duty records were found. Please verify the file format and availability codes."
        )

    if df_qual.empty:
        st.warning(
            "No pilot qualification data was parsed from QUAL.xml. Please ensure the file is valid."
        )

    if df_acts.empty or df_qual.empty:
        st.stop()

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

    # -------------------------------
    # Compute pair overlaps
    # -------------------------------
    pair_results = []
    for (base, ac), pic_group in pic_df.groupby(["base", "aircraft"]):
        sics = sic_df[(sic_df["base"] == base) & (sic_df["aircraft"] == ac)]
        for _, pic_row in pic_group.iterrows():
            for _, sic_row in sics.iterrows():
                overlap_days = len(set(pic_df[pic_df["employee_id"] == pic_row.employee_id]["date"]).intersection(
                    set(sic_df[sic_df["employee_id"] == sic_row.employee_id]["date"]))
                )
                if overlap_days > 0:
                    pic_identifier = pic_row.get("name")
                    if pd.isna(pic_identifier) or pic_identifier == "":
                        pic_identifier = pic_row.get("employee_id")

                    sic_identifier = sic_row.get("name")
                    if pd.isna(sic_identifier) or sic_identifier == "":
                        sic_identifier = sic_row.get("employee_id")

                    pair_results.append({
                        "PIC": pic_identifier,
                        "SIC": sic_identifier,
                        "Base": base,
                        "Aircraft": ac,
                        "Overlap Days": overlap_days
                    })

    df_pairs = pd.DataFrame(pair_results).sort_values(by="Overlap Days", ascending=False)

    st.success(f"Parsed {len(df_qual)} pilots and {len(df_acts)} duty entries.")
    st.write("### Top Pairings by Overlap")
    st.dataframe(df_pairs, use_container_width=True)

    csv = df_pairs.to_csv(index=False).encode('utf-8')
    st.download_button("Download Pairing Results (CSV)", csv, "pairings.csv", "text/csv")
