import pandas as pd
import re
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


DATE_PATTERN = re.compile(r"\d{4}-\d{2}-\d{2}$")
TIME_PATTERN = re.compile(r"\d{2}:\d{2}$")


def parse_acts_line(line):
    """Return a list of duty entries for a single ACTS line.

    Standard lines contain a single duty date, while some represent a span with
    explicit start/end dates. We return one entry per calendar date covered by
    the duty span. If the line contains a relevant duty code (A/D) but no
    recognizable dates, an empty list is returned.
    """

    parts = line.split()
    if len(parts) < 8:
        return None

    emp_id = parts[0]
    raw_code = parts[3].upper()
    code = "D" if raw_code.startswith("DRAFT") else raw_code
    base = parts[4]

    if code not in ["A", "D"]:
        return None

    date_indices = [i for i, token in enumerate(parts[7:], start=7) if DATE_PATTERN.match(token)]
    if not date_indices:
        return []

    start_idx = date_indices[0]
    start_date = pd.to_datetime(parts[start_idx], errors="coerce")
    start_time = (
        parts[start_idx + 1]
        if start_idx + 1 < len(parts) and TIME_PATTERN.match(parts[start_idx + 1])
        else None
    )

    if len(date_indices) > 1:
        end_idx = date_indices[1]
        end_date = pd.to_datetime(parts[end_idx], errors="coerce")
        end_time = (
            parts[end_idx + 1]
            if end_idx + 1 < len(parts) and TIME_PATTERN.match(parts[end_idx + 1])
            else None
        )
    else:
        end_date = start_date
        end_time = None

    if pd.isna(start_date) or pd.isna(end_date):
        return []

    if end_date < start_date:
        end_date = start_date

    # Some off-duty spans end in the early hours of the following day (e.g.,
    # 07:00–06:59). Those should count only for the start date, not the next day.
    if (
        end_date == start_date + pd.Timedelta(days=1)
        and end_time is not None
        and end_time <= "06:59"
    ):
        end_date = start_date

    dates = pd.date_range(start=start_date, end=end_date, freq="D")
    return [
        {"employee_id": emp_id, "date": single.date(), "duty": code, "base": base}
        for single in dates
    ]


def categorise_aircraft(raw_aircraft):
    """Map raw aircraft strings to the target aircraft families we report on."""

    if pd.isna(raw_aircraft):
        return None

    value = str(raw_aircraft).strip().upper()
    if value.startswith("L450"):
        return "Embraer"
    if value.startswith("EMB") or value.startswith("E"):  # Embraer family
        return "Embraer"
    if value.startswith("CJ3"):
        return "CJ3"
    if value.startswith("CJ2"):
        return "CJ2"
    return None


def initials_from_name(name):
    """Return uppercase initials from a full name string.

    If the value is missing or contains no alpha characters, ``None`` is
    returned so downstream consumers can fall back gracefully (e.g., to
    employee_id).
    """

    if pd.isna(name):
        return None

    parts = re.findall(r"[A-Za-z]+", str(name))
    initials = "".join(part[0].upper() for part in parts if part)
    return initials or None


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


def load_restrictions(xlsx_file):
    try:
        df = pd.read_excel(restrictions_file, sheet_name=0, header=0)
    except ImportError:
        st.error(
            "Reading Excel restrictions requires the optional 'openpyxl' dependency. "
            "Please install it (pip install openpyxl) and try again."
        )
        return None

    def normalise(col_name):
        if col_name is None:
            return ""
        cleaned = re.sub(r"[^a-z0-9]+", " ", str(col_name).strip().lower())
        return " ".join(cleaned.split())

    column_aliases = {
        "pilot last name": "last_name",
        "last name": "last_name",
        "pilot initials": "initials",
        "initials": "initials",
        "status": "status",
        "restriction": "restriction_text",
        "restriction text": "restriction_text",
        "ok to fly as f o": "ok_fo",
        "ok to fly as f/o": "ok_fo",
        "ok to fly as fo": "ok_fo",
    }

    renamed_columns = {}
    for col in df.columns:
        target = column_aliases.get(normalise(col))
        if target:
            renamed_columns[col] = target

    df = df.rename(columns=renamed_columns)

    normalized_available = {column_aliases.get(normalise(col), normalise(col)) for col in df.columns}
    required_columns = ["initials", "status", "restriction_text"]
    missing_cols = [col for col in required_columns if col not in normalized_available]
    if missing_cols:
        st.error(
            "Restrictions file is missing expected columns: "
            + ", ".join(missing_cols)
            + " (found: "
            + ", ".join(sorted(normalized_available))
            + ")"
        )
        return None

    df = df[df.get("status", "").fillna("").str.upper() == "RESTRICTION"]

    # Extract disallowed initials list (comma/space separated)
    df["restricted_initials"] = (
        df.get("restriction_text", pd.Series(dtype=str))
        .fillna("")
        .str.replace("Do not fly with", "", case=False)
        .str.replace("Do not crew together without vetting with DP", "", case=False)
        .str.replace("Do not crew on flights to Europe PIC or SIC", "", case=False)
        .str.replace("(", "")
        .str.replace(")", "")
        .str.replace("and", ",")
        .str.replace("/", ",")
        .apply(lambda x: [i.strip().upper() for i in re.split(r"[ ,]+", x) if len(i.strip()) > 1])
    )

    return df


def build_restriction_map(df_restrictions):
    restriction_map = {}
    for _, row in df_restrictions.iterrows():
        pic_init = str(row["initials"]).upper()
        restricted = set(row["restricted_initials"])
        restriction_map[pic_init] = restricted
    return restriction_map


def find_valid_pairs(pics, sics, restriction_map):
    valid_pairs = []
    invalid_pairs = []

    for pic in pics:
        restricted_sics = restriction_map.get(pic, set())

        for sic in sics:
            if sic in restricted_sics:
                invalid_pairs.append((pic, sic))
            else:
                valid_pairs.append((pic, sic))

    return valid_pairs, invalid_pairs

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
restrictions_file = st.file_uploader(
    "Upload Crewing Restrictions Excel",
    type=["xlsx"],
)

restrictions_df = None
restriction_map = {}

if restrictions_file:
    restrictions_df = load_restrictions(restrictions_file)
    if restrictions_df is not None:
        restriction_map = build_restriction_map(restrictions_df)

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
    invalid_date_lines = 0
    text = acts_file.read().decode("utf-8")
    for line in text.splitlines():
        entries = parse_acts_line(line)
        if entries is None:
            continue
        if not entries:
            invalid_date_lines += 1
            continue
        acts_data.extend(entries)

    df_acts = pd.DataFrame(
        acts_data,
        columns=["employee_id", "date", "duty", "base"],
    )

    df_acts["date"] = pd.to_datetime(df_acts["date"], errors="coerce").dt.date
    invalid_dates = df_acts["date"].isna().sum()
    if invalid_dates or invalid_date_lines:
        skipped = invalid_dates + invalid_date_lines
        st.warning(
            f"{skipped} duty entries had unrecognized dates and were skipped."
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

    # -------------------------------
    # Debug view for a single date
    # -------------------------------
    st.write("### Debug: Pilot details for a specific date")
    debug_date = st.date_input(
        "Choose a date to inspect",
        value=min_date,
        min_value=min_date,
        max_value=max_date,
        key="debug_date",
    )

    day_slice = merged[merged["date"] == debug_date].copy()

    if day_slice.empty:
        st.info("No duty entries found on the selected date.")
    else:
        day_slice["initials"] = day_slice["name"].apply(initials_from_name)
        day_slice["initials"] = day_slice["initials"].fillna(day_slice["employee_id"])

        columns = [
            "duty",
            "aircraft_family",
            "seat",
            "initials",
            "employee_id",
            "name",
            "base",
        ]

        display_df = (
            day_slice[columns]
            .rename(
                columns={
                    "duty": "Duty",
                    "aircraft_family": "Aircraft",
                    "seat": "Seat",
                    "initials": "Initials",
                    "employee_id": "Employee ID",
                    "name": "Name",
                    "base": "Base",
                }
            )
            .sort_values(["Duty", "Aircraft", "Seat", "Initials"])
        )

        pics = (
            day_slice[(day_slice["seat"] == "PIC")]["initials"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.upper()
            .tolist()
        )
        sics = (
            day_slice[(day_slice["seat"] == "SIC")]["initials"]
            .dropna()
            .astype(str)
            .str.strip()
            .str.upper()
            .tolist()
        )

        tab_a, tab_d, tab_restrictions = st.tabs(["A days", "D days", "Restrictions"])

        with tab_a:
            a_details = display_df[display_df["Duty"] == "A"]
            if a_details.empty:
                st.info("No A duty pilots on this date.")
            else:
                st.dataframe(a_details, use_container_width=True)

        with tab_d:
            d_details = display_df[display_df["Duty"] == "D"]
            if d_details.empty:
                st.info("No D duty pilots on this date.")
            else:
                st.dataframe(d_details, use_container_width=True)

        with tab_restrictions:
            if not restriction_map:
                st.info("Upload Crewing Restrictions Excel to enable restriction analysis.")
            else:
                restricted_pics_today = [p for p in pics if p in restriction_map]
                restricted_sics_today = [
                    s for s in sics if any(s in restriction_map[p] for p in restriction_map)
                ]

                st.subheader("Crew Restriction Summary")
                st.write(f"Restricted PICs today: {len(restricted_pics_today)}")
                st.write(f"Restricted SICs today: {len(restricted_sics_today)}")

                st.write("PICs with restrictions today:")
                if restricted_pics_today:
                    st.dataframe(pd.DataFrame(restricted_pics_today, columns=["PIC"]))
                else:
                    st.info("No PIC restrictions matched today.")

                st.write("SICs affected by restrictions today:")
                if restricted_sics_today:
                    st.dataframe(pd.DataFrame(restricted_sics_today, columns=["SIC"]))
                else:
                    st.info("No SICs are blocked by restrictions today.")

                valid_pairs, invalid_pairs = find_valid_pairs(pics, sics, restriction_map)

                st.write("Valid PIC/SIC pairings:")
                if valid_pairs:
                    st.dataframe(pd.DataFrame(valid_pairs, columns=["PIC", "SIC"]))
                else:
                    st.info("No valid pairings available based on current restrictions.")

                if invalid_pairs:
                    st.warning("Restricted PIC/SIC combinations:")
                    st.dataframe(pd.DataFrame(invalid_pairs, columns=["PIC", "SIC"]))

                if not valid_pairs and "CJ2" in day_slice["aircraft_family"].dropna().unique():
                    st.error(
                        "⚠ No valid PIC/SIC combinations available for CJ2 today due to restrictions."
                    )
