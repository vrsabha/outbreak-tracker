"""Tests for outbreak cleaning and reconciliation."""

from __future__ import annotations

from pathlib import Path

import pandas as pd

from transform.cleaner import (
    merge_sources,
    normalize_country,
    normalize_strain,
    reconcile_records,
    write_outputs,
)


def test_normalization_handles_country_and_strain_aliases() -> None:
    """Common historical labels should map to canonical values."""
    assert normalize_country("Zaire") == "DRC"
    assert normalize_country("Democratic Republic of the Congo") == "DRC"
    assert normalize_strain("EBOV", disease="Ebola") == "Zaire"
    assert normalize_strain("Zaire ebolavirus", disease="Ebola") == "Zaire"


def test_reconcile_prefers_latest_source_and_flags_conflict(tmp_path: Path) -> None:
    """Most recent source should win while material conflicts are documented."""
    source = pd.DataFrame(
        [
            {
                "disease": "Ebola",
                "country": "Zaire",
                "date": "1976-01-01",
                "strain": "EBOV",
                "confirmed_cases": 318,
                "suspected_cases": None,
                "deaths": 280,
                "source": "CDC",
                "retrieved_at": "2026-01-01T00:00:00Z",
                "updated_at": "2024-01-01T00:00:00Z",
                "is_provisional": False,
            },
            {
                "disease": "Ebola",
                "country": "Democratic Republic of the Congo",
                "date": "1976-01-01",
                "strain": "Zaire ebolavirus",
                "confirmed_cases": 280,
                "suspected_cases": None,
                "deaths": 250,
                "source": "WHO",
                "retrieved_at": "2026-01-01T00:00:00Z",
                "updated_at": "2025-01-01T00:00:00Z",
                "is_provisional": False,
            },
        ]
    )
    merged = merge_sources([source])
    changelog = tmp_path / "changelog.md"

    output = reconcile_records(merged, changelog_path=changelog)

    assert len(output) == 1
    assert output.loc[0, "country"] == "DRC"
    assert output.loc[0, "source_primary"] == "WHO"
    assert bool(output.loc[0, "data_conflict"])
    assert "conflicting confirmed_cases" in changelog.read_text(encoding="utf-8")


def test_write_outputs_creates_master_and_disease_csvs(tmp_path: Path) -> None:
    """Cleaner should write master and per-disease extracts."""
    df = pd.DataFrame(
        [
            {"disease": "Ebola", "country": "DRC"},
            {"disease": "Mpox", "country": "Global"},
        ]
    )

    write_outputs(
        df,
        processed_dir=tmp_path,
        master_csv=tmp_path / "outbreaks_clean.csv",
        dry_run=False,
    )

    assert (tmp_path / "outbreaks_clean.csv").exists()
    assert (tmp_path / "ebola.csv").exists()
    assert (tmp_path / "mpox.csv").exists()

