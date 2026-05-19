"""CDC Ebola outbreak history scraper."""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime, timezone
from io import StringIO
from pathlib import Path
from typing import Dict, List, Optional

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from requests import Response, Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)

CONFIG = {
    "outbreak_history_url": "https://www.cdc.gov/ebola/outbreaks/index.html",
    "timeout_seconds": 20,
    "raw_dir": Path("data/raw"),
    "user_agent": "outbreak-tracker/1.0 (+https://github.com/outbreak-tracker)",
}


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)
def fetch_url(session: Session, url: str) -> Response:
    """Fetch a CDC URL with retries."""
    LOGGER.info("Requesting CDC URL: %s", url)
    response = session.get(url, timeout=CONFIG["timeout_seconds"])
    response.raise_for_status()
    return response


def save_raw_response(content: bytes, stem: str) -> Path:
    """Save a CDC raw response to disk."""
    CONFIG["raw_dir"].mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = CONFIG["raw_dir"] / f"cdc_{stem}_{timestamp}.html"
    try:
        path.write_bytes(content)
    except OSError as exc:
        fallback_dir = Path(tempfile.gettempdir()) / "outbreak-tracker" / "data" / "raw"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        path = fallback_dir / f"cdc_{stem}_{timestamp}.html"
        path.write_bytes(content)
        LOGGER.warning("Raw CDC archive fell back to %s after write failure: %s", path, exc)
    return path


def scrape_cdc_ebola_history(disease: Optional[str] = None) -> pd.DataFrame:
    """Scrape CDC Ebola outbreak history records.

    Args:
        disease: Optional disease filter. Non-Ebola filters return an empty frame.

    Returns:
        Standardized CDC records.
    """
    if disease and "ebola" not in disease.lower():
        LOGGER.info("CDC historical scraper only supports Ebola; skipped %s", disease)
        return pd.DataFrame()

    with requests.Session() as session:
        session.headers.update({"User-Agent": CONFIG["user_agent"]})
        try:
            response = fetch_url(session, CONFIG["outbreak_history_url"])
        except requests.RequestException as exc:
            LOGGER.error("CDC scrape failed: %s", exc)
            return pd.DataFrame()

    save_raw_response(response.content, "ebola_outbreak_history")
    return parse_cdc_history(response.text, CONFIG["outbreak_history_url"])


def parse_cdc_history(html: str, source_url: str) -> pd.DataFrame:
    """Parse CDC history page tables into standardized records."""
    records = _records_from_tables(html, source_url)
    if not records:
        records = _records_from_text(html, source_url)
    LOGGER.info("Parsed %s CDC historical records", len(records))
    return pd.DataFrame(records)


def _records_from_tables(html: str, source_url: str) -> List[Dict[str, object]]:
    """Extract records from HTML tables."""
    records: List[Dict[str, object]] = []
    try:
        tables = pd.read_html(StringIO(html), flavor="lxml")
    except (ImportError, ValueError) as exc:
        LOGGER.warning("CDC table parsing fell back to text parser: %s", exc)
        return records

    for table in tables:
        normalized_columns = {_normalize_column(column): column for column in table.columns}
        if not any("case" in key for key in normalized_columns):
            continue
        for _, row in table.iterrows():
            record = _record_from_row(row, normalized_columns, source_url)
            if record:
                records.append(record)
    return records


def _record_from_row(
    row: pd.Series,
    columns: Dict[str, object],
    source_url: str,
) -> Optional[Dict[str, object]]:
    """Build a standardized record from one CDC table row."""
    country = _first_matching_value(row, columns, ("country", "location", "affected countries"))
    year = _first_matching_value(row, columns, ("year", "date"))
    cases = _first_matching_value(row, columns, ("case", "total cases"))
    deaths = _first_matching_value(row, columns, ("death",))
    strain = _first_matching_value(row, columns, ("species", "virus", "strain"))
    if not country and not year:
        return None

    ambiguous_fields = []
    confirmed_cases = _to_int(cases)
    death_count = _to_int(deaths)
    if confirmed_cases is None:
        ambiguous_fields.append("confirmed_cases")
    if death_count is None:
        ambiguous_fields.append("deaths")

    date_value = _parse_year_or_date(year)
    return {
        "disease": "Ebola",
        "country": str(country).strip() if country else "Unknown",
        "date": date_value,
        "strain": str(strain).strip() if strain else "Unknown",
        "confirmed_cases": confirmed_cases,
        "suspected_cases": None,
        "deaths": death_count,
        "source": "CDC",
        "source_url": source_url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": date_value,
        "is_provisional": False,
        "notes": "CDC Ebola outbreak history",
        "ambiguous_fields": "; ".join(ambiguous_fields),
    }


def _records_from_text(html: str, source_url: str) -> List[Dict[str, object]]:
    """Fallback text parser for CDC pages without parseable tables."""
    soup = BeautifulSoup(html, "lxml")
    text = soup.get_text(" ", strip=True)
    records: List[Dict[str, object]] = []
    pattern = re.compile(
        r"(?P<year>19[7-9][0-9]|20[0-9][0-9]).{0,120}?"
        r"(?P<country>Democratic Republic of the Congo|DRC|Zaire|Sudan|Uganda|"
        r"Gabon|Guinea|Liberia|Sierra Leone|Congo).{0,120}?"
        r"(?P<cases>[0-9][0-9,]*)\s+cases?.{0,80}?"
        r"(?P<deaths>[0-9][0-9,]*)\s+deaths?",
        flags=re.IGNORECASE,
    )
    for match in pattern.finditer(text):
        records.append(
            {
                "disease": "Ebola",
                "country": match.group("country"),
                "date": f"{match.group('year')}-01-01",
                "strain": "Unknown",
                "confirmed_cases": _to_int(match.group("cases")),
                "suspected_cases": None,
                "deaths": _to_int(match.group("deaths")),
                "source": "CDC",
                "source_url": source_url,
                "retrieved_at": datetime.now(timezone.utc).isoformat(),
                "updated_at": f"{match.group('year')}-01-01",
                "is_provisional": False,
                "notes": "CDC text fallback parse",
                "ambiguous_fields": "",
            }
        )
    return records


def _normalize_column(column: object) -> str:
    """Normalize table column names for matching."""
    return re.sub(r"[^a-z0-9]+", " ", str(column).lower()).strip()


def _first_matching_value(
    row: pd.Series,
    columns: Dict[str, object],
    keys: tuple[str, ...],
) -> Optional[object]:
    """Return the first row value whose normalized column contains a key."""
    for key in keys:
        for normalized, original in columns.items():
            if key in normalized:
                value = row.get(original)
                if pd.notna(value):
                    return value
    return None


def _to_int(value: object) -> Optional[int]:
    """Convert a loose numeric value to int."""
    if value is None or pd.isna(value):
        return None
    match = re.search(r"-?[0-9][0-9,]*", str(value))
    return int(match.group(0).replace(",", "")) if match else None


def _parse_year_or_date(value: object) -> Optional[str]:
    """Parse a CDC year or date value."""
    if value is None or pd.isna(value):
        return None
    text = str(value)
    year_match = re.search(r"\b(19[7-9][0-9]|20[0-9][0-9])\b", text)
    if year_match:
        return f"{year_match.group(1)}-01-01"
    try:
        return date_parser.parse(text, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError, TypeError):
        return None
