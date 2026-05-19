"""Data quality validation for cleaned outbreak datasets."""

from __future__ import annotations

import logging
import tempfile
import warnings
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, List, Optional

import pandas as pd

LOGGER = logging.getLogger(__name__)

CONFIG = {
    "report_path": Path("data/processed/quality_report.txt"),
    "controlled_countries": {
        "Angola",
        "Bangladesh",
        "Benin",
        "Bolivia",
        "Brazil",
        "Burundi",
        "Cameroon",
        "Canada",
        "Central African Republic",
        "China",
        "Congo",
        "Cote d'Ivoire",
        "DRC",
        "Dominican Republic",
        "Equatorial Guinea",
        "Ethiopia",
        "France",
        "Gabon",
        "Germany",
        "Ghana",
        "Guinea",
        "Haiti",
        "India",
        "Indonesia",
        "Italy",
        "Kenya",
        "Liberia",
        "Malawi",
        "Mexico",
        "Mozambique",
        "Nigeria",
        "Peru",
        "Philippines",
        "Republic of the Congo",
        "Sierra Leone",
        "South Africa",
        "South Sudan",
        "Spain",
        "Sudan",
        "Tanzania",
        "Uganda",
        "United Kingdom",
        "United States",
        "Vietnam",
        "Zimbabwe",
        "Global",
        "Unknown",
    },
}


@dataclass(frozen=True)
class QualityIssue:
    """Represents a single data quality issue."""

    severity: str
    check: str
    message: str
    rows: int


def _add_issue(
    issues: List[QualityIssue],
    severity: str,
    check: str,
    message: str,
    rows: int,
) -> None:
    """Append and warn about a quality issue."""
    if rows <= 0:
        return
    issue = QualityIssue(severity=severity, check=check, message=message, rows=rows)
    issues.append(issue)
    warnings.warn(message, RuntimeWarning, stacklevel=2)
    LOGGER.warning("%s: %s (%s rows)", check, message, rows)


def validate_dataframe(
    df: pd.DataFrame,
    report_path: Optional[Path] = None,
    controlled_countries: Optional[Iterable[str]] = None,
) -> List[QualityIssue]:
    """Run data quality checks and write a text report.

    Args:
        df: Cleaned outbreak dataset to validate.
        report_path: Optional destination for the report.
        controlled_countries: Optional controlled country vocabulary.

    Returns:
        A list of quality issues. Soft failures are warnings, not exceptions.
    """
    report_destination = report_path or CONFIG["report_path"]
    countries = set(controlled_countries or CONFIG["controlled_countries"])
    data = df.copy()
    issues: List[QualityIssue] = []

    for column in ("confirmed_cases", "suspected_cases", "deaths", "cfr"):
        if column in data.columns:
            data[column] = pd.to_numeric(data[column], errors="coerce")

    negative_columns = ["confirmed_cases", "suspected_cases", "deaths"]
    negative_mask = pd.Series(False, index=data.index)
    for column in negative_columns:
        if column in data.columns:
            negative_mask = negative_mask | (data[column] < 0)
    _add_issue(
        issues,
        "error",
        "non_negative_counts",
        "Case and death counts must not be negative.",
        int(negative_mask.sum()),
    )

    if {"deaths", "confirmed_cases"}.issubset(data.columns):
        impossible_deaths = data["deaths"] > data["confirmed_cases"]
        _add_issue(
            issues,
            "error",
            "deaths_not_above_cases",
            "Deaths exceed confirmed cases for one or more records.",
            int(impossible_deaths.sum()),
        )

    if "date" in data.columns:
        parsed_dates = pd.to_datetime(data["date"], errors="coerce")
        invalid_dates = parsed_dates.isna()
        future_dates = parsed_dates > pd.Timestamp.now(tz=None) + pd.Timedelta(days=7)
        _add_issue(
            issues,
            "warning",
            "valid_dates",
            "One or more records have invalid outbreak dates.",
            int(invalid_dates.sum()),
        )
        _add_issue(
            issues,
            "warning",
            "chronological_dates",
            "One or more records have implausible future outbreak dates.",
            int(future_dates.fillna(False).sum()),
        )

    if "cfr" in data.columns:
        invalid_cfr = data["cfr"].notna() & ~data["cfr"].between(0, 1)
        _add_issue(
            issues,
            "error",
            "cfr_range",
            "CFR values must be between 0 and 1.",
            int(invalid_cfr.sum()),
        )

    if "country" in data.columns:
        unknown_countries = ~data["country"].fillna("Unknown").isin(countries)
        _add_issue(
            issues,
            "warning",
            "controlled_country_vocabulary",
            "Country names outside the controlled vocabulary were found.",
            int(unknown_countries.sum()),
        )

    duplicate_subset = [
        column
        for column in ("disease", "country", "date", "strain", "source_primary")
        if column in data.columns
    ]
    if duplicate_subset:
        duplicates = data.duplicated(subset=duplicate_subset, keep=False)
        _add_issue(
            issues,
            "warning",
            "duplicate_outbreak_records",
            "Potential duplicate outbreak records were found.",
            int(duplicates.sum()),
        )

    write_quality_report(df=data, issues=issues, report_path=report_destination)
    print(render_quality_report(data, issues))
    return issues


def render_quality_report(df: pd.DataFrame, issues: List[QualityIssue]) -> str:
    """Render a human-readable quality report."""
    lines = [
        "Outbreak Tracker Data Quality Report",
        "=" * 43,
        f"Records checked: {len(df)}",
        f"Issues found: {len(issues)}",
        "",
    ]
    if not issues:
        lines.append("All validation checks passed.")
    else:
        for issue in issues:
            lines.append(
                f"[{issue.severity.upper()}] {issue.check}: "
                f"{issue.message} ({issue.rows} rows)"
            )
    return "\n".join(lines)


def write_quality_report(
    df: pd.DataFrame,
    issues: List[QualityIssue],
    report_path: Path,
) -> None:
    """Write the quality report to disk."""
    try:
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_quality_report(df, issues), encoding="utf-8")
    except OSError as exc:
        report_path = Path(tempfile.gettempdir()) / "outbreak-tracker" / "data" / "processed" / "quality_report.txt"
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(render_quality_report(df, issues), encoding="utf-8")
        LOGGER.warning("Quality report fell back to %s after write failure: %s", report_path, exc)
    LOGGER.info("Wrote data quality report to %s", report_path)
