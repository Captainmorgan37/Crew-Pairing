import pandas as pd
import streamlit as st
import xml.etree.ElementTree as ET


CATEGORY_LABELS = {
    ("Embraer", "SIC"): "Embraer SICs",
    ("Embraer", "PIC"): "Embraer PICs",
    ("CJ3", "SIC"): "CJ3 SICs",
    ("CJ3", "PIC"): "CJ3 PICs",
    ("CJ2", "SIC"): "CJ2 SICs",
    ("CJ2", "PIC"): "CJ2 PICs",
}


def categorise_aircraft(raw_aircraft):
    """Map raw aircraft strings to the target aircraft families we report on."""

    if pd.isna(raw_aircraft):
        return None

    value = str(raw_aircraft).strip().upper()
    if value.startswith("EMB") or value.startswith("E"):  # Embraer family
        return "Embraer"
    if value.startswith("CJ3"):
        return "CJ3"
    if value.startswith("CJ2"):
        return "CJ2"
    return None


def build_daily_summary(merged_df, duty_code):
    """Return a wide dataframe of crew counts per day for a duty code (A or D)."""

    filtered = merged_df[
        (merged_df["duty"] == duty_code)
        & merged_df["aircraft_family"].notna()
        & merged_df["seat"].isin(["PIC", "SIC"])
    ].copy()

    if filtered.empty:
        return pd.DataFrame(columns=["Date", *CATEGORY_LABELS.values()])

    filtered["category"] = filtered.apply(
        lambda row: CATEGORY_LABELS.get((row["aircraft_family"], row["seat"])), axis=1
    )
    filtered = filtered.dropna(subset=["category"])

    counts = (
        filtered.groupby(["date", "category"], as_index=False)["employee_id"].nunique()
    )

    pivoted = counts.pivot_table(
        index="date",
        columns="category",
        values="employee_id",
        fill_value=0,
    ).reset_index()

    for col in CATEGORY_LABELS.values():
        if col not in pivoted.columns:
            pivoted[col] = 0

    ordered_columns = ["date", *CATEGORY_LABELS.values()]
    pivoted = pivoted[ordered_columns].sort_values("date")
    pivoted = pivoted.rename(columns={"date": "Date"})
    return pivoted

st.set_page_config(page_title="Crew Availability Overview", layout="wide")
st.title("🧭 Crew Availability Overview")

st.write(
    "Upload QUAL.xml and an ACTS file to see how many PICs and SICs are on A and D days, "
    "broken down by Embraer, CJ3, and CJ2 fleets."
)

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
        code = parts[3].upper()
        base = parts[4]
        date = parts[7]
        if code not in ["A", "D"]:
            continue
        acts_data.append(
            {
                "employee_id": emp_id,
                "date": date,
                "duty": code,
                "base": base,
            }
        )

    df_acts = pd.DataFrame(
        acts_data,
        columns=["employee_id", "date", "duty", "base"],
    )

    df_acts["date"] = pd.to_datetime(df_acts["date"], errors="coerce").dt.date
    invalid_dates = df_acts["date"].isna().sum()
    if invalid_dates:
        st.warning(
            f"{invalid_dates} duty entries had unrecognized dates and were skipped."
        )
    df_acts = df_acts.dropna(subset=["date"])

    if df_acts.empty:
        st.warning(
            "No usable ACTS duty records were found. Please verify the file format and availability codes (A or D)."
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

    merged["aircraft_family"] = merged["aircraft"].apply(categorise_aircraft)

    min_date = merged["date"].min()
    max_date = merged["date"].max()

    st.write("### Date range")
    selected_range = st.date_input(
        "Select the range of dates to include",
        value=(min_date, max_date),
        min_value=min_date,
        max_value=max_date,
    )

    if not isinstance(selected_range, tuple) or len(selected_range) != 2:
        st.error("Please select both a start and end date.")
        st.stop()

    start_date, end_date = selected_range

    filtered = merged[(merged["date"] >= start_date) & (merged["date"] <= end_date)]
    if filtered.empty:
        st.warning("No duty entries fall within the selected date range.")
        st.stop()

    a_days = build_daily_summary(filtered, "A")
    d_days = build_daily_summary(filtered, "D")

    st.success(
        f"Parsed {len(df_qual)} pilots and {len(filtered)} duty entries within the selected range."
    )

    st.write("### Counts on A days")
    st.dataframe(a_days, use_container_width=True)

    st.write("### Counts on D days")
    st.dataframe(d_days, use_container_width=True)

    st.download_button(
        "Download A day summary (CSV)", a_days.to_csv(index=False).encode("utf-8"), "a_days.csv", "text/csv"
    )
    st.download_button(
        "Download D day summary (CSV)", d_days.to_csv(index=False).encode("utf-8"), "d_days.csv", "text/csv"
    )
