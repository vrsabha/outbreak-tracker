"""ECDC rapid risk assessment fetcher."""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from requests import Response, Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)

CONFIG = {
    "assessment_pages": {
        "Ebola": [
            "https://www.ecdc.europa.eu/en/publications-data/rapid-risk-assessment-outbreak-ebola-virus-disease-west-africa-13th-update-16",
            "https://www.ecdc.europa.eu/en/publications-data/RRA-ebola-virus-disease-outbreak-DRC-fifth-update",
        ],
        "Mpox": [
            "https://www.ecdc.europa.eu/en/news-events/ecdc-releases-first-update-its-rapid-risk-assessment-monkeypox-outbreak",
            "https://www.ecdc.europa.eu/en/infectious-disease-topics/mpox/rapid-scientific-advice-public-health-measures-mpox-2024-2025",
        ],
        "Cholera": [
            "https://www.ecdc.europa.eu/en/publications-data/rapid-risk-assessment-risk-travel-associated-cholera-dominican-republic",
        ],
        "Bird Flu": [
            "https://www.ecdc.europa.eu/en/publications-data/rapid-risk-assessment-potential-resurgence-highly-pathogenic-h5n1-avian-influenza",
            "https://www.ecdc.europa.eu/en/publications-data/rapid-risk-assessment-influenza-ah7n9-china-12-april-2013",
        ],
    },
    "timeout_seconds": 25,
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
    """Fetch an ECDC page or PDF with retries."""
    LOGGER.info("Requesting ECDC URL: %s", url)
    response = session.get(url, timeout=CONFIG["timeout_seconds"])
    response.raise_for_status()
    return response


def save_raw_response(content: bytes, stem: str, suffix: str) -> Path:
    """Save an ECDC raw response to disk."""
    CONFIG["raw_dir"].mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    path = CONFIG["raw_dir"] / f"ecdc_{_slugify(stem)}_{timestamp}{suffix}"
    try:
        path.write_bytes(content)
    except OSError as exc:
        fallback_dir = Path(tempfile.gettempdir()) / "outbreak-tracker" / "data" / "raw"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        path = fallback_dir / f"ecdc_{_slugify(stem)}_{timestamp}{suffix}"
        path.write_bytes(content)
        LOGGER.warning("Raw ECDC archive fell back to %s after write failure: %s", path, exc)
    return path


def fetch_ecdc_assessments(disease: Optional[str] = None) -> pd.DataFrame:
    """Fetch ECDC risk assessment pages and linked PDFs.

    Args:
        disease: Optional disease name to limit the fetch.

    Returns:
        Standardized ECDC records.
    """
    selected_pages = _selected_pages(disease)
    records: List[Dict[str, object]] = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": CONFIG["user_agent"]})
        for disease_name, urls in selected_pages.items():
            for url in urls:
                try:
                    response = fetch_url(session, url)
                except requests.RequestException as exc:
                    LOGGER.error("ECDC fetch failed for %s: %s", url, exc)
                    continue
                save_raw_response(response.content, url.rsplit("/", 1)[-1], ".html")
                records.append(parse_assessment_page(response.text, url, disease_name))
                for pdf_url in extract_pdf_links(response.text, url):
                    try:
                        pdf_response = fetch_url(session, pdf_url)
                    except requests.RequestException as exc:
                        LOGGER.warning("ECDC PDF fetch failed for %s: %s", pdf_url, exc)
                        continue
                    save_raw_response(pdf_response.content, pdf_url.rsplit("/", 1)[-1], ".pdf")
    return pd.DataFrame(records)


def parse_assessment_page(html: str, url: str, disease_name: str) -> Dict[str, object]:
    """Parse summary text from one ECDC assessment page."""
    soup = BeautifulSoup(html, "lxml")
    title = _first_text(soup, "h1") or disease_name
    text = soup.get_text(" ", strip=True)
    counts = _extract_counts_and_cfr(text)
    date_value = _extract_date(soup, text)
    return {
        "disease": disease_name,
        "country": _extract_country(text),
        "date": date_value,
        "strain": _extract_strain(text, disease_name),
        "confirmed_cases": counts.get("confirmed_cases"),
        "suspected_cases": None,
        "deaths": counts.get("deaths"),
        "source": "ECDC",
        "source_url": url,
        "retrieved_at": datetime.now(timezone.utc).isoformat(),
        "updated_at": date_value,
        "is_provisional": "ongoing" in text.lower() or "current" in text.lower(),
        "notes": title,
        "ambiguous_fields": "; ".join(counts.get("ambiguous_fields", [])),
    }


def extract_pdf_links(html: str, base_url: str) -> List[str]:
    """Extract linked PDF URLs from an ECDC assessment page."""
    soup = BeautifulSoup(html, "lxml")
    links: List[str] = []
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        if href.lower().endswith(".pdf"):
            absolute = urljoin(base_url, href)
            if absolute not in links:
                links.append(absolute)
    LOGGER.info("Found %s ECDC PDF links for %s", len(links), base_url)
    return links


def _selected_pages(disease: Optional[str]) -> Dict[str, List[str]]:
    """Return the ECDC pages selected for this run."""
    if not disease:
        return CONFIG["assessment_pages"]
    for disease_name, urls in CONFIG["assessment_pages"].items():
        if disease.lower() in disease_name.lower() or disease_name.lower() in disease.lower():
            return {disease_name: urls}
    return {}


def _first_text(soup: BeautifulSoup, selector: str) -> Optional[str]:
    """Return the first non-empty text for a selector."""
    element = soup.select_one(selector)
    return element.get_text(" ", strip=True) if element else None


def _extract_counts_and_cfr(text: str) -> Dict[str, object]:
    """Extract counts and flag ambiguous fields from ECDC text."""
    compact = re.sub(r"\s+", " ", text)
    fields: Dict[str, object] = {"ambiguous_fields": []}
    cases = re.findall(r"([0-9][0-9,]*)\s+(?:confirmed\s+)?cases", compact, re.IGNORECASE)
    deaths = re.findall(r"([0-9][0-9,]*)\s+deaths", compact, re.IGNORECASE)
    fields["confirmed_cases"] = int(cases[0].replace(",", "")) if cases else None
    fields["deaths"] = int(deaths[0].replace(",", "")) if deaths else None
    if len(set(cases)) > 1:
        fields["ambiguous_fields"].append("confirmed_cases")
    if len(set(deaths)) > 1:
        fields["ambiguous_fields"].append("deaths")
    if not cases:
        fields["ambiguous_fields"].append("confirmed_cases")
    if not deaths:
        fields["ambiguous_fields"].append("deaths")
    return fields


def _extract_date(soup: BeautifulSoup, text: str) -> Optional[str]:
    """Extract a publication date from ECDC page text."""
    time_element = soup.find("time")
    candidates = []
    if time_element:
        candidates.append(str(time_element.get("datetime") or time_element.get_text(" ", strip=True)))
    candidates.extend(
        re.findall(
            r"\b\d{1,2}\s+(?:January|February|March|April|May|June|July|August|"
            r"September|October|November|December)\s+\d{4}\b",
            text,
            flags=re.IGNORECASE,
        )
    )
    for candidate in candidates:
        try:
            return date_parser.parse(candidate, fuzzy=True).date().isoformat()
        except (ValueError, OverflowError, TypeError):
            continue
    return None


def _extract_country(text: str) -> str:
    """Extract a country or region label from ECDC text."""
    if re.search(r"\bglobal\b|\bworldwide\b|\bEU/EEA\b", text, re.IGNORECASE):
        return "Global"
    match = re.search(
        r"\b(?:Democratic Republic of the Congo|DRC|Uganda|Guinea|Liberia|"
        r"Sierra Leone|Dominican Republic|China|Germany|United States)\b",
        text,
        flags=re.IGNORECASE,
    )
    return match.group(0) if match else "Unknown"


def _extract_strain(text: str, disease_name: str) -> str:
    """Extract a strain, species, or clade from ECDC text."""
    lower_text = text.lower()
    patterns = [
        ("h5n1", "A(H5N1)"),
        ("h5n8", "A(H5N8)"),
        ("h7n9", "A(H7N9)"),
        ("clade ib", "Clade Ib"),
        ("clade i", "Clade I"),
        ("sudan virus", "Sudan"),
        ("zaire", "Zaire"),
    ]
    for needle, label in patterns:
        if needle in lower_text:
            return label
    return disease_name


def _slugify(value: str) -> str:
    """Convert a value into a filesystem-safe slug."""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "response"
