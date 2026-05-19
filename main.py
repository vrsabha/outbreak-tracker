"""Command-line entry point for the outbreak-tracker data pipeline."""

from __future__ import annotations

import argparse
import logging
import os
import subprocess
import sys
from dataclasses import dataclass
from typing import List, Optional

import pandas as pd

from ingestion.cdc_scraper import scrape_cdc_ebola_history
from ingestion.ecdc_fetcher import fetch_ecdc_assessments
from ingestion.who_scraper import scrape_who_outbreaks
from transform.cfr_calculator import add_cfr_columns
from transform.cleaner import merge_sources, reconcile_records, write_outputs
from transform.validator import validate_dataframe

LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True)
class PipelineSummary:
    """Summary metrics emitted at the end of a pipeline run."""

    records_ingested: int
    records_output: int
    conflicts_found: int
    quality_issues: int


def configure_logging(verbose: bool = False) -> None:
    """Configure process-wide logging."""
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
    )


def run_tests() -> None:
    """Run the test suite before the pipeline collects live data."""
    LOGGER.info("Running pytest before pipeline execution")
    env = os.environ.copy()
    env.setdefault("COVERAGE_FILE", os.path.join(os.environ.get("TEMP", "."), "outbreak_tracker_coverage"))
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "--cov=transform", "--cov-fail-under=80"],
        check=False,
        env=env,
        text=True,
    )
    if result.returncode != 0:
        raise RuntimeError("Tests failed; pipeline execution was stopped.")


def run_pipeline(disease: Optional[str] = None, dry_run: bool = False) -> PipelineSummary:
    """Run ingestion, transformation, validation, and output steps.

    Args:
        disease: Optional disease filter.
        dry_run: When true, do not write cleaned CSV outputs.

    Returns:
        Pipeline summary metrics.
    """
    LOGGER.info("Starting outbreak-tracker pipeline")
    source_frames: List[pd.DataFrame] = [
        scrape_who_outbreaks(disease=disease),
        scrape_cdc_ebola_history(disease=disease),
        fetch_ecdc_assessments(disease=disease),
    ]
    records_ingested = sum(len(frame) for frame in source_frames if frame is not None)
    LOGGER.info("Ingested %s source records", records_ingested)

    merged = merge_sources(source_frames)
    reconciled = reconcile_records(merged)
    calculated = add_cfr_columns(reconciled)
    quality_issues = validate_dataframe(calculated)
    write_outputs(calculated, dry_run=dry_run)

    summary = PipelineSummary(
        records_ingested=records_ingested,
        records_output=len(calculated),
        conflicts_found=int(calculated.get("data_conflict", pd.Series(dtype=bool)).sum()),
        quality_issues=len(quality_issues),
    )
    LOGGER.info("Pipeline complete: %s", summary)
    return summary


def parse_args(argv: Optional[List[str]] = None) -> argparse.Namespace:
    """Parse command-line arguments."""
    parser = argparse.ArgumentParser(description="Run the outbreak-tracker pipeline.")
    parser.add_argument("--disease", help="Run the pipeline for one disease only.")
    parser.add_argument("--dry-run", action="store_true", help="Run without writing CSV outputs.")
    parser.add_argument("--skip-tests", action="store_true", help="Skip pre-flight pytest run.")
    parser.add_argument("--verbose", action="store_true", help="Enable debug logging.")
    return parser.parse_args(argv)


def main(argv: Optional[List[str]] = None) -> int:
    """Run the command-line interface."""
    args = parse_args(argv)
    configure_logging(verbose=args.verbose)
    try:
        if not args.skip_tests:
            run_tests()
        summary = run_pipeline(disease=args.disease, dry_run=args.dry_run)
    except RuntimeError as exc:
        LOGGER.error("%s", exc)
        return 1

    print(
        "\nPipeline summary\n"
        f"- Records ingested: {summary.records_ingested}\n"
        f"- Records output: {summary.records_output}\n"
        f"- Conflicts found: {summary.conflicts_found}\n"
        f"- Quality issues flagged: {summary.quality_issues}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
