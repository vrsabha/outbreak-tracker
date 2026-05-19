"""Tests for data quality validation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from transform.validator import validate_dataframe


def test_validator_reports_realistic_soft_and_hard_failures(tmp_path: Path) -> None:
    """Validator should warn and report data issues without throwing."""
    df = pd.DataFrame(
        [
            {
                "disease": "Ebola",
                "country": "DRC",
                "date": "1976-01-01",
                "strain": "Zaire",
                "confirmed_cases": 318,
                "suspected_cases": None,
                "deaths": 280,
                "cfr": 0.88,
                "source_primary": "CDC",
            },
            {
                "disease": "Cholera",
                "country": "Atlantis",
                "date": "not-a-date",
                "strain": "Vibrio cholerae",
                "confirmed_cases": 12,
                "suspected_cases": -1,
                "deaths": 20,
                "cfr": 1.2,
                "source_primary": "WHO",
            },
        ]
    )
    report = tmp_path / "quality_report.txt"

    issues = validate_dataframe(df, report_path=report)

    checks = {issue.check for issue in issues}
    assert "non_negative_counts" in checks
    assert "deaths_not_above_cases" in checks
    assert "valid_dates" in checks
    assert "cfr_range" in checks
    assert "controlled_country_vocabulary" in checks
    assert report.exists()


def test_validator_accepts_clean_data(tmp_path: Path) -> None:
    """A clean dataset should produce no issues."""
    df = pd.DataFrame(
        [
            {
                "disease": "Mpox",
                "country": "Global",
                "date": "2024-08-14",
                "strain": "Clade Ib",
                "confirmed_cases": 100,
                "suspected_cases": 10,
                "deaths": 2,
                "cfr": 0.02,
                "source_primary": "WHO",
            }
        ]
    )

    issues = validate_dataframe(df, report_path=tmp_path / "report.txt")

    assert issues == []

