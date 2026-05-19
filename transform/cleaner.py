"""Cleaning and reconciliation utilities for outbreak records."""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional

import pandas as pd

LOGGER = logging.getLogger(__name__)

CONFIG = {
    "processed_dir": Path("data/processed"),
    "changelog_path": Path("data/changelog.md"),
    "master_csv": Path("data/processed/outbreaks_clean.csv"),
    "conflict_threshold": 0.10,
}

OUTPUT_COLUMNS = [
    "disease",
    "country",
    "date",
    "strain",
    "confirmed_cases",
    "suspected_cases",
    "deaths",
    "cfr",
    "cfr_ci_lower",
    "cfr_ci_upper",
    "statistically_unreliable",
    "is_provisional",
    "data_conflict",
    "source_primary",
    "source",
    "source_url",
    "retrieved_at",
    "updated_at",
    "notes",
    "ambiguous_fields",
]

COUNTRY_ALIASES = {
    "zaire": "DRC",
    "democratic republic of congo": "DRC",
    "democratic republic of the congo": "DRC",
    "dr congo": "DRC",
    "drc": "DRC",
    "congo, democratic republic of the": "DRC",
    "usa": "United States",
    "united states of america": "United States",
    "uk": "United Kingdom",
    "cote d ivoire": "Cote d'Ivoire",
    "côte d'ivoire": "Cote d'Ivoire",
}

STRAIN_ALIASES = {
    "ebola zaire": "Zaire",
    "ebov": "Zaire",
    "zaire ebolavirus": "Zaire",
    "zaire": "Zaire",
    "sudan virus": "Sudan",
    "sudan ebolavirus": "Sudan",
    "sudv": "Sudan",
    "bundibugyo": "Bundibugyo",
    "bundibugyo virus": "Bundibugyo",
    "reston": "Reston",
    "tai forest": "Tai Forest",
    "taï forest": "Tai Forest",
    "mpox clade i": "Clade I",
    "clade i": "Clade I",
    "clade ib": "Clade Ib",
    "clade ii": "Clade II",
    "clade iib": "Clade IIb",
    "h5n1": "A(H5N1)",
    "h5n5": "A(H5N5)",
    "h7n9": "A(H7N9)",
}


def normalize_country(country: object) -> str:
    """Normalize a country value to the project vocabulary."""
    if country is None or pd.isna(country) or not str(country).strip():
        return "Unknown"
    value = re.sub(r"\s+", " ", str(country).strip())
    alias_key = value.lower().replace("’", "'")
    alias_key = re.sub(r"[^a-z0-9'\s]", " ", alias_key)
    alias_key = re.sub(r"\s+", " ", alias_key).strip()
    return COUNTRY_ALIASES.get(alias_key, value)


def normalize_strain(strain: object, disease: object = None, notes: object = None) -> str:
    """Normalize disease strain or clade names."""
    candidates = [strain, disease, notes]
    text = " ".join(str(item) for item in candidates if item is not None and pd.notna(item))
    if not text.strip():
        return "Unknown"
    lower_text = text.lower()
    for alias, canonical in STRAIN_ALIASES.items():
        if alias in lower_text:
            return canonical
    if "cholera" in lower_text:
        return "Vibrio cholerae"
    if "mpox" in lower_text or "monkeypox" in lower_text:
        return "Unknown clade"
    if "avian" in lower_text or "influenza" in lower_text or "bird flu" in lower_text:
        return "Avian influenza"
    return str(strain).strip() if strain is not None and pd.notna(strain) else "Unknown"


def merge_sources(source_frames: Iterable[pd.DataFrame]) -> pd.DataFrame:
    """Merge source DataFrames into one standardized table."""
    frames = [frame.copy() for frame in source_frames if frame is not None and not frame.empty]
    if not frames:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    merged = pd.concat(frames, ignore_index=True, sort=False)
    for column in OUTPUT_COLUMNS:
        if column not in merged.columns:
            merged[column] = pd.NA

    merged["country"] = merged["country"].apply(normalize_country)
    merged["strain"] = merged.apply(
        lambda row: normalize_strain(row.get("strain"), row.get("disease"), row.get("notes")),
        axis=1,
    )
    merged["disease"] = merged["disease"].fillna("Unknown").astype(str).str.strip()
    merged["date"] = pd.to_datetime(merged["date"], errors="coerce").dt.date.astype("string")

    for column in ("confirmed_cases", "suspected_cases", "deaths"):
        merged[column] = pd.to_numeric(merged[column], errors="coerce").astype("Int64")

    for column in ("retrieved_at", "updated_at"):
        merged[column] = pd.to_datetime(merged[column], errors="coerce", utc=True)
    merged["updated_at"] = merged["updated_at"].fillna(merged["retrieved_at"])
    merged["retrieved_at"] = merged["retrieved_at"].fillna(pd.Timestamp.now(tz="UTC"))
    merged["is_provisional"] = merged["is_provisional"].fillna(False).astype(bool)
    return merged


def reconcile_records(
    df: pd.DataFrame,
    changelog_path: Optional[Path] = None,
    conflict_threshold: float = CONFIG["conflict_threshold"],
) -> pd.DataFrame:
    """Reconcile duplicate source records and document material conflicts."""
    if df.empty:
        return pd.DataFrame(columns=OUTPUT_COLUMNS)

    output_rows: List[Dict[str, object]] = []
    conflict_entries: List[str] = []
    keys = ["disease", "country", "date", "strain"]
    grouped = df.sort_values("updated_at", ascending=False).groupby(keys, dropna=False)

    for key_values, group in grouped:
        primary = group.iloc[0].copy()
        primary["source_primary"] = primary.get("source", "Unknown")
        conflict_columns = [
            column
            for column in ("confirmed_cases", "suspected_cases", "deaths")
            if _has_material_conflict(group[column], conflict_threshold)
        ]
        primary["data_conflict"] = bool(conflict_columns)
        if conflict_columns:
            conflict_entries.append(_format_conflict_entry(key_values, group, conflict_columns))
        output_rows.append(primary.to_dict())

    reconciled = pd.DataFrame(output_rows)
    if conflict_entries:
        append_changelog(conflict_entries, changelog_path or CONFIG["changelog_path"])
    LOGGER.info(
        "Reconciled %s source rows into %s outbreak records with %s conflicts",
        len(df),
        len(reconciled),
        len(conflict_entries),
    )
    return reconciled.reindex(columns=OUTPUT_COLUMNS)


def _has_material_conflict(values: pd.Series, threshold: float) -> bool:
    """Return whether numeric source values differ by more than a threshold."""
    numeric = pd.to_numeric(values, errors="coerce").dropna().astype(float)
    if len(numeric.unique()) <= 1:
        return False
    max_value = numeric.max()
    min_value = numeric.min()
    denominator = max(max_value, 1.0)
    return ((max_value - min_value) / denominator) > threshold


def _format_conflict_entry(
    key_values: object,
    group: pd.DataFrame,
    conflict_columns: List[str],
) -> str:
    """Render a changelog entry for a reconciled conflict."""
    disease, country, date_value, strain = key_values
    lines = [
        f"- {disease} | {country} | {date_value} | {strain}: "
        f"conflicting {', '.join(conflict_columns)}.",
    ]
    for _, row in group.iterrows():
        values = ", ".join(
            f"{column}={row.get(column)}" for column in conflict_columns
        )
        lines.append(
            f"  - {row.get('source', 'Unknown')} "
            f"updated {row.get('updated_at')}: {values}"
        )
    return "\n".join(lines)


def append_changelog(entries: List[str], changelog_path: Path) -> None:
    """Append reconciliation notes to the data changelog."""
    changelog_path.parent.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).isoformat()
    existing = changelog_path.read_text(encoding="utf-8") if changelog_path.exists() else ""
    header = "# Data Changelog\n\n" if not existing else ""
    body = f"## {timestamp}\n\n" + "\n".join(entries) + "\n\n"
    try:
        changelog_path.write_text(header + existing + body, encoding="utf-8")
    except OSError as exc:
        fallback_path = Path(tempfile.gettempdir()) / "outbreak-tracker" / "data" / "changelog.md"
        fallback_path.parent.mkdir(parents=True, exist_ok=True)
        existing = fallback_path.read_text(encoding="utf-8") if fallback_path.exists() else ""
        header = "# Data Changelog\n\n" if not existing else ""
        fallback_path.write_text(header + existing + body, encoding="utf-8")
        LOGGER.warning("Changelog fell back to %s after write failure: %s", fallback_path, exc)
        changelog_path = fallback_path
    LOGGER.info("Documented %s data judgment calls in %s", len(entries), changelog_path)


def write_outputs(
    df: pd.DataFrame,
    processed_dir: Path = CONFIG["processed_dir"],
    master_csv: Path = CONFIG["master_csv"],
    dry_run: bool = False,
) -> None:
    """Write the master dataset and disease-specific CSV extracts."""
    if dry_run:
        LOGGER.info("Dry run enabled; cleaned outputs were not written")
        return

    try:
        processed_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(master_csv, index=False)
    except OSError as exc:
        processed_dir = Path(tempfile.gettempdir()) / "outbreak-tracker" / "data" / "processed"
        master_csv = processed_dir / "outbreaks_clean.csv"
        processed_dir.mkdir(parents=True, exist_ok=True)
        df.to_csv(master_csv, index=False)
        LOGGER.warning("Processed outputs fell back to %s after write failure: %s", processed_dir, exc)
    for disease, group in df.groupby("disease", dropna=False):
        slug = re.sub(r"[^a-z0-9]+", "_", str(disease).lower()).strip("_") or "unknown"
        group.to_csv(processed_dir / f"{slug}.csv", index=False)
    LOGGER.info("Wrote cleaned outputs to %s", processed_dir)
