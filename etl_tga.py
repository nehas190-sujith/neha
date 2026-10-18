from pathlib import Path
from io import StringIO, BytesIO
from urllib.parse import urljoin
import hashlib
import json
import re
import sqlite3
import zipfile

import pandas as pd
import requests

# ============================================================
# MED INTEL - TGA + INTERNATIONAL MEDICINE SHORTAGE ETL
# ============================================================
# AUTOMATIC SOURCES
#   TGA (Australia)
#   FDA / openFDA (United States)
#   EMA (Europe)
#   MHLW (Japan current supply-status workbook)
#
# MANUAL SOURCE
#   Health Canada export ZIPs placed in data/manual/
#
# OUTPUTS
#   data/tga_current.csv
#   data/tga_tableau.csv
#   data/global_shortage_records.csv
#   data/global_shortage_trends.csv
#   data/global_source_summary.csv
#   database/medintel.db
# ============================================================

BASE_DIR = Path(__file__).resolve().parent
DATA_DIR = BASE_DIR / "data"
MANUAL_DIR = DATA_DIR / "manual"
DATABASE_DIR = BASE_DIR / "database"
for folder in (DATA_DIR, MANUAL_DIR, DATABASE_DIR):
    folder.mkdir(parents=True, exist_ok=True)

TGA_CURRENT_CSV = DATA_DIR / "tga_current.csv"
TGA_TABLEAU_CSV = DATA_DIR / "tga_tableau.csv"
GLOBAL_RECORDS_CSV = DATA_DIR / "global_shortage_records.csv"
GLOBAL_TRENDS_CSV = DATA_DIR / "global_shortage_trends.csv"
GLOBAL_SUMMARY_CSV = DATA_DIR / "global_source_summary.csv"
MHLW_DOWNLOAD = DATA_DIR / "mhlw_current.xlsx"
DATABASE_FILE = DATABASE_DIR / "medintel.db"

TGA_CURRENT_URL = "https://apps.tga.gov.au/shortages/search?exportType=Excel&shortagetype=All"
TGA_ARCHIVE_URL = "https://apps.tga.gov.au/shortages/search?exportType=CSVExportArchive&shortagetype=All"
FDA_URL = "https://api.fda.gov/drug/shortages.json"
EMA_URL = "https://www.ema.europa.eu/en/documents/report/shortages-output-json-report_en.json"
MHLW_PAGE = "https://www.mhlw.go.jp/stf/seisakunitsuite/bunya/kenkou_iryou/iryou/kouhatu-iyaku/04_00003.html"

HEADERS = {
    "User-Agent": "MedIntel Medicine Shortage Dashboard - university research project"
}

TGA_COLUMN_MAP = {
    "ARTG ID": "artg_id",
    "ARTG name": "artg_name",
    "Active ingredients": "active_ingredients",
    "Dosage form": "dosage_form",
    "Quantity of active ingredients": "strength",
    "Sponsor": "sponsor",
    "Phone": "sponsor_phone",
    "Shortage status": "shortage_status",
    "Supply impact start date": "supply_start_date",
    "Supply impact end date": "supply_end_date",
    "Deletion from market": "deletion_from_market",
    "Shortage impact rating": "impact_rating",
    "Availability": "availability",
    "Reason": "reason",
    "Management action": "management_action",
    "Last updated": "last_updated",
}

TGA_EXPECTED = [
    "artg_id", "artg_name", "active_ingredients", "dosage_form", "strength",
    "sponsor", "sponsor_phone", "shortage_status", "supply_start_date",
    "supply_end_date", "deletion_from_market", "impact_rating", "availability",
    "reason", "management_action", "last_updated"
]

GLOBAL_COLUMNS = [
    "shortage_record_id", "source_record_id", "country_region", "regulator",
    "medicine_name", "active_ingredient", "manufacturer", "dosage_form",
    "strength", "therapeutic_category", "shortage_status", "source_status",
    "shortage_reason", "impact_level", "availability", "start_date",
    "expected_end_date", "resolution_date", "first_published_date",
    "last_updated_date", "duration_days", "is_current", "is_anticipated",
    "is_resolved", "is_discontinued", "management_action",
    "alternatives_available", "tier_3_status", "source_url", "dataset_source",
    "record_scope", "start_date_basis", "resolution_date_basis"
]

# ============================================================
# GENERIC HELPERS
# ============================================================

def clean_text(value):
    if value is None:
        return ""
    if isinstance(value, float) and pd.isna(value):
        return ""
    if isinstance(value, (list, tuple, set)):
        return "; ".join(clean_text(x) for x in value if clean_text(x))
    if isinstance(value, dict):
        return json.dumps(value, ensure_ascii=False)
    text = str(value).replace("\xa0", " ")
    return re.sub(r"\s+", " ", text).strip()


def sha_id(*values, length=24):
    text = "|".join(clean_text(v) for v in values)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def safe_date(value, dayfirst=False):
    text = clean_text(value)
    if not text:
        return pd.NaT
    return pd.to_datetime(text, format="mixed", dayfirst=dayfirst, errors="coerce")


def parse_date_series(series, dayfirst=False):
    return pd.to_datetime(
        series.replace("", pd.NA),
        format="mixed",
        dayfirst=dayfirst,
        errors="coerce",
    )


def normalise_status(value):
    text = clean_text(value).lower()
    if not text:
        return "Unknown"
    if "avoided" in text:
        return "Avoided"
    if "reversed" in text:
        return "Reversed"
    if "to be discontinued" in text:
        return "Anticipated Discontinuation"
    if "anticipat" in text or "expected shortage" in text:
        return "Anticipated"
    if "resolved" in text or "restored" in text:
        return "Resolved"
    if "discontinu" in text:
        return "Discontinued"
    if "actual shortage" in text or "ongoing" in text or "current" in text:
        return "Current"
    if "供給停止" in text or "限定出荷" in text:
        return "Current"
    if "通常出荷" in text:
        return "Normal"
    return clean_text(value).title()


def finalise_global(df):
    df = df.copy()
    for col in GLOBAL_COLUMNS:
        if col not in df.columns:
            df[col] = ""

    date_cols = [
        "start_date", "expected_end_date", "resolution_date",
        "first_published_date", "last_updated_date"
    ]
    for col in date_cols:
        df[col] = pd.to_datetime(df[col], errors="coerce")

    today = pd.Timestamp.today().normalize()
    df["duration_days"] = (df["resolution_date"] - df["start_date"]).dt.days
    unresolved = df["resolution_date"].isna() & df["start_date"].notna()
    df.loc[unresolved, "duration_days"] = (
        today - df.loc[unresolved, "start_date"]
    ).dt.days
    df.loc[df["duration_days"] < 0, "duration_days"] = pd.NA

    df["is_current"] = (df["shortage_status"] == "Current").astype(int)
    df["is_anticipated"] = (df["shortage_status"] == "Anticipated").astype(int)
    df["is_resolved"] = (df["shortage_status"] == "Resolved").astype(int)
    df["is_discontinued"] = (df["shortage_status"] == "Discontinued").astype(int)
    return df[GLOBAL_COLUMNS]


def normalise_header(text):
    return re.sub(r"[\W_]+", "", clean_text(text).lower(), flags=re.UNICODE)


def find_column(df, aliases):
    lookup = {normalise_header(c): c for c in df.columns}
    for alias in aliases:
        key = normalise_header(alias)
        if key in lookup:
            return lookup[key]
    return None


def get_value(row, df, aliases):
    col = find_column(df, aliases)
    return row.get(col, "") if col else ""

# ============================================================
# TGA
# ============================================================

def download_tga(url, label):
    print(f"\nDownloading {label}...")
    response = requests.get(url, headers=HEADERS, timeout=120)
    response.raise_for_status()
    text = response.content.decode("utf-8-sig", errors="replace")
    lines = text.splitlines()

    header_index = None
    for i, line in enumerate(lines):
        if "ARTG ID" in line and "ARTG name" in line:
            header_index = i
            break
    if header_index is None:
        raise ValueError("Could not locate TGA CSV header.")

    df = pd.read_csv(
        StringIO("\n".join(lines[header_index:])),
        dtype=str,
        keep_default_na=False,
    )
    print(f"Downloaded {len(df):,} TGA rows.")
    return df


def clean_tga(df, dataset_type):
    print(f"Cleaning TGA {dataset_type} data...")
    df = df.copy()
    df.columns = [clean_text(c) for c in df.columns]
    df = df.rename(columns=TGA_COLUMN_MAP)

    for col in TGA_EXPECTED:
        if col not in df.columns:
            df[col] = ""

    date_cols = {"supply_start_date", "supply_end_date", "deletion_from_market", "last_updated"}
    for col in df.columns:
        if col not in date_cols and df[col].dtype == "object":
            df[col] = df[col].map(clean_text)
    for col in date_cols:
        df[col] = parse_date_series(df[col], dayfirst=True)

    if dataset_type == "archive":
        df["shortage_status"] = "Resolved"
        df.loc[df["deletion_from_market"].notna(), "shortage_status"] = "Discontinued"
        df["availability"] = "Archived"
        df["management_action"] = ""
        df["last_updated"] = pd.NaT

    df["status_normalised"] = df["shortage_status"].map(normalise_status)
    df["impact_normalised"] = df["impact_rating"].map(lambda x: clean_text(x).title())
    df["availability_normalised"] = df["availability"].map(lambda x: clean_text(x).title())

    df["shortage_duration_days"] = (df["supply_end_date"] - df["supply_start_date"]).dt.days
    df.loc[df["shortage_duration_days"] < 0, "shortage_duration_days"] = pd.NA
    today = pd.Timestamp.today().normalize()
    df["days_until_expected_supply"] = (df["supply_end_date"] - today).dt.days

    df["is_current_shortage"] = (df["status_normalised"] == "Current").astype(int)
    df["is_anticipated_shortage"] = (df["status_normalised"] == "Anticipated").astype(int)
    df["is_resolved"] = (df["status_normalised"] == "Resolved").astype(int)
    df["is_discontinued"] = (df["status_normalised"] == "Discontinued").astype(int)
    df["is_critical"] = (df["impact_normalised"] == "Critical").astype(int)
    df["is_unavailable"] = (df["availability_normalised"] == "Unavailable").astype(int)
    df["has_management_action"] = (df["management_action"].fillna("").astype(str).str.strip().str.len() > 0).astype(int)

    impact_scores = {"Low": 1, "Medium": 2, "High": 3, "Critical": 4}
    df["impact_score"] = df["impact_normalised"].map(impact_scores).fillna(0).astype(int)

    df["duration_category"] = pd.cut(
        df["shortage_duration_days"],
        bins=[-1, 30, 90, 180, 365, float("inf")],
        labels=["0-30 days", "31-90 days", "91-180 days", "181-365 days", "365+ days"],
    ).astype("object").fillna("Unknown")

    df["country"] = "Australia"
    df["regulator"] = "TGA"
    df["dataset_type"] = dataset_type
    df["shortage_record_id"] = df.apply(
        lambda r: sha_id(
            r.get("artg_id"), r.get("shortage_status"), r.get("supply_start_date"),
            r.get("supply_end_date"), r.get("deletion_from_market")
        ),
        axis=1,
    )

    before = len(df)
    df = df.drop_duplicates().reset_index(drop=True)
    print(f"Cleaned TGA {dataset_type}: {len(df):,} rows ({before-len(df):,} duplicates removed).")
    return df


def combine_tga(current_df, archive_df):
    current_df = current_df.copy()
    archive_df = archive_df.copy()
    current_df["in_current_tga_export"] = 1
    archive_df["in_current_tga_export"] = 0

    combined = pd.concat([current_df, archive_df], ignore_index=True, sort=False)
    before = len(combined)
    combined = combined.drop_duplicates(subset=["shortage_record_id"], keep="first").reset_index(drop=True)
    print(f"Combined TGA dataset: {len(combined):,} rows ({before-len(combined):,} overlaps removed).")
    return combined


def tga_to_global(df):
    rows = []
    for _, row in df.iterrows():
        is_archive = clean_text(row.get("dataset_type")) == "archive"
        rows.append({
            "shortage_record_id": "TGA-" + clean_text(row.get("shortage_record_id")),
            "source_record_id": clean_text(row.get("artg_id")),
            "country_region": "Australia",
            "regulator": "TGA",
            "medicine_name": clean_text(row.get("artg_name")),
            "active_ingredient": clean_text(row.get("active_ingredients")),
            "manufacturer": clean_text(row.get("sponsor")),
            "dosage_form": clean_text(row.get("dosage_form")),
            "strength": clean_text(row.get("strength")),
            "therapeutic_category": "",
            "shortage_status": normalise_status(row.get("shortage_status")),
            "source_status": clean_text(row.get("shortage_status")),
            "shortage_reason": clean_text(row.get("reason")),
            "impact_level": clean_text(row.get("impact_rating")),
            "availability": clean_text(row.get("availability")),
            "start_date": row.get("supply_start_date"),
            "expected_end_date": pd.NaT if is_archive else row.get("supply_end_date"),
            "resolution_date": row.get("supply_end_date") if is_archive else pd.NaT,
            "first_published_date": pd.NaT,
            "last_updated_date": row.get("last_updated"),
            "management_action": clean_text(row.get("management_action")),
            "alternatives_available": "",
            "tier_3_status": "",
            "source_url": "https://apps.tga.gov.au/shortages/search",
            "dataset_source": "TGA Medicine Shortage Reports Database",
            "record_scope": "Archive" if is_archive else "Current",
            "start_date_basis": "TGA supply impact start date",
            "resolution_date_basis": "TGA archived supply impact end date" if is_archive else "",
        })
    return finalise_global(pd.DataFrame(rows))

# ============================================================
# FDA
# ============================================================

def download_fda_shortages():
    print("\nDownloading FDA shortages...")
    records = []
    limit = 1000
    skip = 0

    while True:
        response = requests.get(
            FDA_URL,
            params={"limit": limit, "skip": skip},
            headers=HEADERS,
            timeout=120,
        )
        if response.status_code == 404:
            break
        response.raise_for_status()
        payload = response.json()
        batch = payload.get("results", [])
        if not batch:
            break

        records.extend(batch)
        total = payload.get("meta", {}).get("results", {}).get("total")
        if isinstance(total, int):
            print(f"FDA records downloaded: {len(records):,}/{total:,}")
        else:
            print(f"FDA records downloaded: {len(records):,}")

        if len(batch) < limit:
            break
        skip += len(batch)
        if isinstance(total, int) and skip >= total:
            break
        if skip > 25000:
            break

    print(f"Downloaded {len(records):,} FDA rows.")
    return records


def fda_to_global(records):
    rows = []
    for item in records:
        status_raw = clean_text(item.get("status"))
        status = normalise_status(status_raw)

        source_id = clean_text(item.get("product_id")) or clean_text(item.get("package_ndc"))
        if not source_id:
            source_id = sha_id(item.get("generic_name"), item.get("company_name"), item.get("presentation"))

        initial = safe_date(item.get("initial_posting_date"))
        change = safe_date(item.get("change_date"))
        discontinued = safe_date(item.get("discontinued_date"))

        resolution = pd.NaT
        resolution_basis = ""
        if status == "Discontinued" and pd.notna(discontinued):
            resolution = discontinued
            resolution_basis = "FDA discontinued_date"
        elif status == "Resolved" and pd.notna(change):
            resolution = change
            resolution_basis = "FDA change_date"

        openfda = item.get("openfda", {}) or {}
        ingredient = clean_text(openfda.get("substance_name")) or clean_text(item.get("generic_name"))

        rows.append({
            "shortage_record_id": "FDA-" + sha_id(source_id, status_raw, initial),
            "source_record_id": source_id,
            "country_region": "United States",
            "regulator": "FDA",
            "medicine_name": clean_text(item.get("proprietary_name")) or clean_text(item.get("generic_name")),
            "active_ingredient": ingredient,
            "manufacturer": clean_text(item.get("company_name")),
            "dosage_form": clean_text(item.get("dosage_form")),
            "strength": clean_text(item.get("strength")),
            "therapeutic_category": clean_text(item.get("therapeutic_category")),
            "shortage_status": status,
            "source_status": status_raw,
            "shortage_reason": clean_text(item.get("shortage_reason")),
            "impact_level": "",
            "availability": clean_text(item.get("availability")),
            "start_date": initial,
            "expected_end_date": pd.NaT,
            "resolution_date": resolution,
            "first_published_date": initial,
            "last_updated_date": safe_date(item.get("update_date")),
            "management_action": clean_text(item.get("related_info")) or clean_text(item.get("resolved_note")),
            "alternatives_available": "",
            "tier_3_status": "",
            "source_url": clean_text(item.get("related_info_link")) or "https://open.fda.gov/apis/drug/drugshortages/",
            "dataset_source": "openFDA Drug Shortages",
            "record_scope": "National",
            "start_date_basis": "FDA initial_posting_date",
            "resolution_date_basis": resolution_basis,
        })

    output = finalise_global(pd.DataFrame(rows))
    print(f"Normalised FDA records: {len(output):,}")
    return output

# ============================================================
# EMA
# ============================================================

def download_ema_shortages():
    print("\nDownloading EMA shortages...")
    response = requests.get(EMA_URL, headers=HEADERS, timeout=120)
    response.raise_for_status()
    payload = response.json()
    if isinstance(payload, dict):
        records = payload.get("data") or payload.get("results") or []
    else:
        records = payload
    print(f"Downloaded {len(records):,} EMA rows.")
    return records


def ema_to_global(records):
    rows = []
    for item in records:
        status_raw = clean_text(item.get("supply_shortage_status"))
        status = normalise_status(status_raw)
        start = safe_date(item.get("start_of_shortage_date"), dayfirst=True)
        published = safe_date(item.get("first_published_date"), dayfirst=True)
        start_basis = "EMA start_of_shortage_date"
        if pd.isna(start):
            start = published
            start_basis = "EMA first_published_date (start unavailable)"

        source_url = clean_text(item.get("shortage_url"))
        source_id = source_url.rsplit("/", 1)[-1] if source_url else sha_id(item.get("medicine_affected"), published)

        resolution = pd.NaT
        resolution_basis = ""
        if status == "Resolved":
            # EMA does not expose a dedicated actual-resolution date in every record.
            # Use last_updated only when a resolved record has no better resolution date.
            candidate = safe_date(item.get("last_updated_date"), dayfirst=True)
            if pd.notna(candidate):
                resolution = candidate
                resolution_basis = "EMA last_updated_date used for resolved record"

        rows.append({
            "shortage_record_id": "EMA-" + sha_id(source_id, status_raw, published),
            "source_record_id": source_id,
            "country_region": "European Union / EEA",
            "regulator": "EMA",
            "medicine_name": clean_text(item.get("medicine_affected")),
            "active_ingredient": clean_text(item.get("international_non_proprietary_name_inn_or_common_name")),
            "manufacturer": "",
            "dosage_form": clean_text(item.get("pharmaceutical_forms_affected")),
            "strength": clean_text(item.get("strengths_affected")),
            "therapeutic_category": clean_text(item.get("therapeutic_area_mesh")),
            "shortage_status": status,
            "source_status": status_raw,
            "shortage_reason": clean_text(item.get("reason_for_shortage")),
            "impact_level": "",
            "availability": "",
            "start_date": start,
            "expected_end_date": safe_date(item.get("expected_resolution_date"), dayfirst=True),
            "resolution_date": resolution,
            "first_published_date": published,
            "last_updated_date": safe_date(item.get("last_updated_date"), dayfirst=True),
            "management_action": clean_text(item.get("expected_resolution")),
            "alternatives_available": clean_text(item.get("availability_of_alternatives")),
            "tier_3_status": "",
            "source_url": source_url,
            "dataset_source": "EMA Medicine Supply Shortages",
            "record_scope": "EMA-assessed European shortage",
            "start_date_basis": start_basis,
            "resolution_date_basis": resolution_basis,
        })

    output = finalise_global(pd.DataFrame(rows))
    print(f"Normalised EMA records: {len(output):,}")
    return output

# ============================================================
# HEALTH CANADA - MULTIPLE EXPORT ZIPS
# ============================================================

def read_health_canada_zips():
    print("\nLoading Health Canada exports...")
    candidates = sorted(MANUAL_DIR.glob("health_canada_*.zip"))
    if not candidates:
        candidates = sorted(MANUAL_DIR.glob("*.zip"))
    if not candidates:
        print("Health Canada: no ZIP files found; skipping.")
        return pd.DataFrame()

    frames = []
    for zip_path in candidates:
        print(f"Reading: {zip_path.name}")
        with zipfile.ZipFile(zip_path, "r") as archive:
            for filename in archive.namelist():
                if not filename.lower().endswith(".csv"):
                    continue

                with archive.open(filename) as file:
                    raw_bytes = file.read()

                # Health Canada puts one metadata/disclaimer row before the real header.
                # skiprows=1 is therefore intentional.
                frame = pd.read_csv(
                    BytesIO(raw_bytes),
                    dtype=str,
                    keep_default_na=False,
                    skiprows=1,
                    encoding="utf-8-sig",
                )

                frame.columns = [clean_text(c) for c in frame.columns]
                frame["_source_file"] = filename
                frame["_source_zip"] = zip_path.name
                frame["_report_type"] = (
                    "Discontinuation" if "discontinuation" in filename.lower() else "Shortage"
                )
                frames.append(frame)
                print(f"  {filename}: {len(frame):,} rows")

    if not frames:
        print("Health Canada ZIPs contained no CSV files.")
        return pd.DataFrame()

    combined = pd.concat(frames, ignore_index=True, sort=False)
    print(f"Health Canada total raw rows: {len(combined):,}")
    print(f"Health Canada ZIP files loaded: {len(candidates)}")
    return combined


def health_canada_to_global(df):
    if df.empty:
        return finalise_global(pd.DataFrame(columns=GLOBAL_COLUMNS))

    print("\nTransforming Health Canada data...")
    rows = []

    for _, row in df.iterrows():
        report_type = clean_text(row.get("_report_type"))
        source_file = clean_text(row.get("_source_file"))
        is_discontinuation = report_type == "Discontinuation"

        report_id = clean_text(get_value(row, df, ["Report ID", "Report Id", "ID"]))
        din = clean_text(get_value(row, df, ["Drug Identification Number", "DIN"]))
        medicine = clean_text(get_value(row, df, ["Brand name", "Brand Name", "Common or Proper name"]))

        status_raw = clean_text(get_value(
            row, df,
            ["Shortage status", "Discontinuation status", "Status"]
        ))
        if not status_raw and is_discontinuation:
            status_raw = "Discontinued"
        status = normalise_status(status_raw)

        anticipated_start = safe_date(get_value(row, df, ["Anticipated start date"]))
        actual_start = safe_date(get_value(row, df, ["Actual start date"]))
        estimated_end = safe_date(get_value(row, df, ["Estimated end date"]))
        actual_end = safe_date(get_value(row, df, ["Actual end date"]))
        discontinuation_date = safe_date(get_value(row, df, ["Discontinuation date"]))
        anticipated_discontinuation = safe_date(get_value(row, df, ["Anticipated discontinuation date"]))

        if is_discontinuation:
            if pd.notna(discontinuation_date):
                start_date = discontinuation_date
                start_basis = "Health Canada discontinuation date"
            else:
                start_date = anticipated_discontinuation
                start_basis = "Health Canada anticipated discontinuation date"
            expected_end_date = pd.NaT
            resolution_date = pd.NaT
        else:
            if pd.notna(actual_start):
                start_date = actual_start
                start_basis = "Health Canada actual start date"
            else:
                start_date = anticipated_start
                start_basis = "Health Canada anticipated start date"
            expected_end_date = estimated_end
            resolution_date = actual_end

        date_created = safe_date(get_value(row, df, ["Date Created"]))
        date_updated = safe_date(get_value(row, df, ["Date Updated", "Updated date"]))

        if report_id:
            source_id = report_id
        else:
            source_id = sha_id(din, medicine, start_date, report_type, source_file)

        rows.append({
            "shortage_record_id": "HC-" + sha_id(source_id, report_type),
            "source_record_id": source_id,
            "country_region": "Canada",
            "regulator": "Health Canada",
            "medicine_name": medicine,
            "active_ingredient": clean_text(get_value(row, df, ["Ingredients", "Active Ingredient(s)", "Active ingredients"])),
            "manufacturer": clean_text(get_value(row, df, ["Company Name", "Company"])),
            "dosage_form": clean_text(get_value(row, df, ["Dosage form(s)", "Dosage form"])),
            "strength": clean_text(get_value(row, df, ["Strength(s)", "Strength"])),
            "therapeutic_category": clean_text(get_value(row, df, ["ATC description", "Anatomical Therapeutical Chemical (ATC) description"])),
            "shortage_status": status,
            "source_status": status_raw,
            "shortage_reason": clean_text(get_value(row, df, ["Reason", "Reason for shortage"])),
            "impact_level": "",
            "availability": "",
            "start_date": start_date,
            "expected_end_date": expected_end_date,
            "resolution_date": resolution_date,
            "first_published_date": date_created,
            "last_updated_date": date_updated,
            "management_action": "",
            "alternatives_available": "",
            "tier_3_status": clean_text(get_value(row, df, ["Tier 3", "Tier 3 Status"])),
            "source_url": "https://healthproductshortages.ca/search",
            "dataset_source": "Health Product Shortages Canada",
            "record_scope": "National - " + report_type,
            "start_date_basis": start_basis,
            "resolution_date_basis": "Health Canada actual end date" if (not is_discontinuation and pd.notna(actual_end)) else "",
        })

    output = finalise_global(pd.DataFrame(rows))
    print(f"Normalised Health Canada records: {len(output):,}")
    print("Health Canada records by status:")
    print(output["shortage_status"].value_counts(dropna=False).to_string())
    return output

# ============================================================
# JAPAN MHLW - AUTOMATIC WORKBOOK + ROBUST HEADER DETECTION
# ============================================================

MHLW_HEADER_TERMS = [
    "品目名", "販売名", "医薬品名", "製品名", "成分名", "一般名", "有効成分",
    "製造販売業者名", "製造販売業者", "企業名", "出荷対応の状況", "出荷対応",
    "供給状況", "出荷量の状況", "出荷量", "規格", "規格単位", "含量"
]


def _row_values_as_strings(raw_df, row_index):
    return [clean_text(v) for v in raw_df.iloc[row_index].tolist()]


def _score_header_values(values):
    joined = "|".join(values)
    return sum(term in joined for term in MHLW_HEADER_TERMS)


def _make_unique_columns(columns):
    counts = {}
    out = []
    for col in columns:
        base = clean_text(col) or "unnamed"
        counts[base] = counts.get(base, 0) + 1
        out.append(base if counts[base] == 1 else f"{base}_{counts[base]}")
    return out


def detect_mhlw_table(workbook_path):
    excel = pd.ExcelFile(workbook_path)
    best = None

    for sheet in excel.sheet_names:
        raw = pd.read_excel(workbook_path, sheet_name=sheet, header=None, dtype=str)
        if raw.empty:
            continue

        max_rows = min(80, len(raw))
        for i in range(max_rows):
            one = _row_values_as_strings(raw, i)
            score_one = _score_header_values(one)
            if best is None or score_one > best[0]:
                best = (score_one, sheet, i, 1, one)

            if i + 1 < max_rows:
                two_a = _row_values_as_strings(raw, i)
                two_b = _row_values_as_strings(raw, i + 1)
                combined = []
                width = max(len(two_a), len(two_b))
                for j in range(width):
                    a = two_a[j] if j < len(two_a) else ""
                    b = two_b[j] if j < len(two_b) else ""
                    combined.append(" ".join(x for x in [a, b] if x).strip())
                score_two = _score_header_values(combined)
                if best is None or score_two > best[0]:
                    best = (score_two, sheet, i, 2, combined)

    if best is None or best[0] < 2:
        raise ValueError("Could not identify the MHLW data header row in any workbook sheet.")

    score, sheet, header_row, header_height, columns = best
    raw = pd.read_excel(workbook_path, sheet_name=sheet, header=None, dtype=str)
    data_start = header_row + header_height
    df = raw.iloc[data_start:].copy().reset_index(drop=True)
    df.columns = _make_unique_columns(columns[:len(df.columns)])
    df = df.dropna(how="all").reset_index(drop=True)

    print(f"MHLW detected sheet: {sheet}")
    print(f"MHLW detected header row: {header_row + 1}")
    print(f"MHLW header score: {score}")
    return df


def download_mhlw_current():
    print("\nFinding latest Japan MHLW Excel...")
    response = requests.get(MHLW_PAGE, headers=HEADERS, timeout=120)
    response.raise_for_status()

    matches = re.findall(
        r'href=["\']([^"\']+\.xlsx(?:\?[^"\']*)?)["\']',
        response.text,
        flags=re.I,
    )
    if not matches:
        raise ValueError("Could not locate an .xlsx link on the MHLW page.")

    # Prefer medicine-supply workbooks when the filename makes that obvious.
    preferred = [m for m in matches if "iyakuhinkyoukyu" in m.lower() or "kyoukyu" in m.lower()]
    chosen = preferred[-1] if preferred else matches[-1]
    excel_url = urljoin(MHLW_PAGE, chosen)
    print(f"MHLW workbook: {excel_url}")

    file_response = requests.get(excel_url, headers=HEADERS, timeout=120)
    file_response.raise_for_status()
    MHLW_DOWNLOAD.write_bytes(file_response.content)

    df = detect_mhlw_table(MHLW_DOWNLOAD)
    print(f"Loaded {len(df):,} Japan MHLW rows after header detection.")
    return df


def mhlw_to_global(df):
    if df.empty:
        return finalise_global(pd.DataFrame(columns=GLOBAL_COLUMNS))

    medicine_col = find_column(df, ["品目名", "販売名", "医薬品名", "製品名"])
    ingredient_col = find_column(df, ["成分名", "一般名", "有効成分"])
    manufacturer_col = find_column(df, ["製造販売業者名", "製造販売業者", "企業名"])
    strength_col = find_column(df, ["規格", "規格単位", "含量"])
    shipping_col = find_column(df, ["出荷対応の状況", "出荷対応", "供給状況"])
    volume_col = find_column(df, ["出荷量の状況", "出荷量"])
    update_col = find_column(df, ["今回掲載時の更新有無", "更新有無", "更新日"])

    if not medicine_col or not shipping_col:
        print("MHLW columns were still not recognised after automatic header detection.")
        print("Columns found:")
        print(list(df.columns))
        return finalise_global(pd.DataFrame(columns=GLOBAL_COLUMNS))

    snapshot = pd.Timestamp.today().normalize()
    rows = []

    for _, row in df.iterrows():
        source_status = clean_text(row.get(shipping_col, ""))
        status = normalise_status(source_status)

        # Only constrained / stopped supply rows are treated as shortage records.
        if status != "Current":
            continue

        medicine = clean_text(row.get(medicine_col, ""))
        manufacturer = clean_text(row.get(manufacturer_col, "")) if manufacturer_col else ""
        ingredient = clean_text(row.get(ingredient_col, "")) if ingredient_col else ""
        strength = clean_text(row.get(strength_col, "")) if strength_col else ""
        availability = clean_text(row.get(volume_col, "")) if volume_col else ""
        update_value = clean_text(row.get(update_col, "")) if update_col else ""
        source_id = sha_id(medicine, manufacturer, strength, source_status)

        rows.append({
            "shortage_record_id": "MHLW-" + source_id,
            "source_record_id": source_id,
            "country_region": "Japan",
            "regulator": "MHLW",
            "medicine_name": medicine,
            "active_ingredient": ingredient,
            "manufacturer": manufacturer,
            "dosage_form": "",
            "strength": strength,
            "therapeutic_category": "",
            "shortage_status": "Current",
            "source_status": source_status,
            "shortage_reason": "",
            "impact_level": "",
            "availability": availability,
            "start_date": pd.NaT,
            "expected_end_date": pd.NaT,
            "resolution_date": pd.NaT,
            "first_published_date": pd.NaT,
            "last_updated_date": snapshot,
            "management_action": update_value,
            "alternatives_available": "",
            "tier_3_status": "",
            "source_url": MHLW_PAGE,
            "dataset_source": "MHLW Medicine Supply Status",
            "record_scope": "Current supply-status snapshot",
            "start_date_basis": "Not available in current MHLW supply-status snapshot",
            "resolution_date_basis": "Not available in current MHLW supply-status snapshot",
        })

    output = finalise_global(pd.DataFrame(rows))
    print(f"Normalised Japan MHLW constrained-supply records: {len(output):,}")
    return output

# ============================================================
# COMBINE + ANALYTICS
# ============================================================

def combine_global(frames):
    usable = [f for f in frames if f is not None and not f.empty]
    if not usable:
        return finalise_global(pd.DataFrame(columns=GLOBAL_COLUMNS))

    combined = pd.concat(usable, ignore_index=True, sort=False)
    before = len(combined)
    combined = combined.drop_duplicates(subset=["shortage_record_id"], keep="first").reset_index(drop=True)
    print(f"\nCombined global dataset: {len(combined):,} rows ({before-len(combined):,} duplicates removed).")
    return finalise_global(combined)


def build_global_trends(global_df):
    if global_df.empty:
        return pd.DataFrame(columns=[
            "month", "regulator", "country_region", "new_shortage_reports",
            "resolved_shortages", "average_duration_days"
        ])

    df = global_df.copy()
    df["trend_start_date"] = df["start_date"].combine_first(df["first_published_date"])
    df["start_month"] = df["trend_start_date"].dt.to_period("M").dt.to_timestamp()
    df["resolution_month"] = df["resolution_date"].dt.to_period("M").dt.to_timestamp()

    new = (
        df.dropna(subset=["start_month"])
        .groupby(["start_month", "regulator", "country_region"], as_index=False)
        .agg(
            new_shortage_reports=("shortage_record_id", "nunique"),
            average_duration_days=("duration_days", "mean"),
        )
        .rename(columns={"start_month": "month"})
    )

    resolved = (
        df.dropna(subset=["resolution_month"])
        .groupby(["resolution_month", "regulator", "country_region"], as_index=False)
        .agg(resolved_shortages=("shortage_record_id", "nunique"))
        .rename(columns={"resolution_month": "month"})
    )

    trends = new.merge(
        resolved,
        on=["month", "regulator", "country_region"],
        how="outer",
    )
    trends["new_shortage_reports"] = trends["new_shortage_reports"].fillna(0).astype(int)
    trends["resolved_shortages"] = trends["resolved_shortages"].fillna(0).astype(int)
    trends["average_duration_days"] = trends["average_duration_days"].round(1)
    return trends.sort_values(["month", "regulator"]).reset_index(drop=True)


def build_source_summary(global_df):
    if global_df.empty:
        return pd.DataFrame(columns=["regulator", "country_region", "shortage_status", "records"])
    return (
        global_df.groupby(["regulator", "country_region", "shortage_status"], dropna=False)
        .agg(records=("shortage_record_id", "nunique"))
        .reset_index()
    )

# ============================================================
# EXPORTS + SQLITE
# ============================================================

def export_csv(df, path):
    output = df.copy()
    for col in output.columns:
        if pd.api.types.is_datetime64_any_dtype(output[col]):
            output[col] = output[col].dt.strftime("%Y-%m-%d")
    output.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"Created: {path}")


def sql_ready(df):
    output = df.copy()
    for col in output.columns:
        if pd.api.types.is_datetime64_any_dtype(output[col]):
            output[col] = output[col].dt.strftime("%Y-%m-%d")
    return output


def create_database(tga_df, global_df, trends_df, summary_df):
    print("\nCreating SQLite database...")
    if DATABASE_FILE.exists():
        DATABASE_FILE.unlink()

    conn = sqlite3.connect(DATABASE_FILE)
    sql_ready(tga_df).to_sql("fact_tga_shortage", conn, if_exists="replace", index=False)
    sql_ready(global_df).to_sql("fact_global_shortage", conn, if_exists="replace", index=False)
    sql_ready(trends_df).to_sql("agg_global_shortage_trends", conn, if_exists="replace", index=False)
    sql_ready(summary_df).to_sql("agg_global_source_summary", conn, if_exists="replace", index=False)

    log = pd.DataFrame({
        "etl_run_time": [pd.Timestamp.now().isoformat()],
        "tga_rows": [len(tga_df)],
        "global_rows": [len(global_df)],
        "regulators_loaded": [", ".join(sorted(global_df["regulator"].dropna().unique())) if not global_df.empty else ""],
    })
    log.to_sql("etl_run_log", conn, if_exists="replace", index=False)

    conn.execute("CREATE INDEX IF NOT EXISTS idx_global_regulator ON fact_global_shortage(regulator)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_global_status ON fact_global_shortage(shortage_status)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_global_start ON fact_global_shortage(start_date)")
    conn.commit()
    conn.close()
    print(f"Created database: {DATABASE_FILE}")

# ============================================================
# MAIN
# ============================================================

def main():
    print("=" * 72)
    print("MED INTEL - TGA + INTERNATIONAL MEDICINE SHORTAGE ETL")
    print("=" * 72)

    # TGA
    tga_current = clean_tga(download_tga(TGA_CURRENT_URL, "current TGA shortages"), "current")
    tga_archive = clean_tga(download_tga(TGA_ARCHIVE_URL, "TGA archive"), "archive")
    tga_all = combine_tga(tga_current, tga_archive)
    export_csv(tga_current, TGA_CURRENT_CSV)
    export_csv(tga_all, TGA_TABLEAU_CSV)
    tga_global = tga_to_global(tga_all)

    # FDA
    try:
        fda_global = fda_to_global(download_fda_shortages())
    except Exception as error:
        print(f"\nFDA import failed: {error}")
        fda_global = finalise_global(pd.DataFrame(columns=GLOBAL_COLUMNS))

    # EMA
    try:
        ema_global = ema_to_global(download_ema_shortages())
    except Exception as error:
        print(f"\nEMA import failed: {error}")
        ema_global = finalise_global(pd.DataFrame(columns=GLOBAL_COLUMNS))

    # Health Canada - reads ALL export ZIPs in data/manual/
    try:
        canada_global = health_canada_to_global(read_health_canada_zips())
    except Exception as error:
        print(f"\nHealth Canada import failed: {error}")
        canada_global = finalise_global(pd.DataFrame(columns=GLOBAL_COLUMNS))

    # Japan MHLW
    try:
        japan_global = mhlw_to_global(download_mhlw_current())
    except Exception as error:
        print(f"\nJapan MHLW import failed: {error}")
        japan_global = finalise_global(pd.DataFrame(columns=GLOBAL_COLUMNS))

    global_df = combine_global([
        tga_global,
        fda_global,
        ema_global,
        canada_global,
        japan_global,
    ])

    trends_df = build_global_trends(global_df)
    summary_df = build_source_summary(global_df)

    print("\nCreating Tableau-ready outputs...")
    export_csv(global_df, GLOBAL_RECORDS_CSV)
    export_csv(trends_df, GLOBAL_TRENDS_CSV)
    export_csv(summary_df, GLOBAL_SUMMARY_CSV)
    create_database(tga_all, global_df, trends_df, summary_df)

    print("\n" + "=" * 72)
    print("ETL COMPLETE")
    print("=" * 72)
    print(f"\nTGA Tableau file:\n{TGA_TABLEAU_CSV}")
    print(f"\nGlobal detailed file:\n{GLOBAL_RECORDS_CSV}")
    print(f"\nGlobal trends file:\n{GLOBAL_TRENDS_CSV}")
    print(f"\nGlobal summary file:\n{GLOBAL_SUMMARY_CSV}")
    print(f"\nSQLite database:\n{DATABASE_FILE}")
    print(f"\nTotal global records: {len(global_df):,}")

    if not global_df.empty:
        print("\nRows by regulator:")
        print(global_df["regulator"].value_counts().to_string())

    print("\nNOTE:")
    print("- Health Canada reads every health_canada_*.zip file in data/manual/.")
    print("- Japan MHLW is a current supply-status snapshot. Without historical start dates,")
    print("  Japan records are included in the detailed dataset and source summary but may")
    print("  not appear in the monthly new-shortage trend series.")


if __name__ == "__main__":
    main()
