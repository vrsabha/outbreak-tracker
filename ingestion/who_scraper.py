"""WHO Disease Outbreak News scraper."""

from __future__ import annotations

import logging
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional
from urllib.parse import urljoin

import pandas as pd
import requests
from bs4 import BeautifulSoup
from dateutil import parser as date_parser
from requests import Response, Session
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

LOGGER = logging.getLogger(__name__)

CONFIG = {
    "base_url": "https://www.who.int",
    "listing_url": "https://www.who.int/emergencies/disease-outbreak-news",
    "search_params": {"query": ""},
    "diseases": ("Ebola", "Mpox", "Cholera", "Bird Flu"),
    "timeout_seconds": 20,
    "raw_dir": Path("data/raw"),
    "user_agent": "outbreak-tracker/1.0 (+https://github.com/outbreak-tracker)",
    "max_articles_per_disease": 8,
    "seed_article_urls": {
        "Ebola": [
            "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON602",
            "https://www.who.int/emergencies/disease-outbreak-news/item/2025-DON580",
        ],
        "Mpox": [
            "https://www.who.int/emergencies/disease-outbreak-news/item/2026-DON595",
        ],
        "Cholera": [
            "https://www.who.int/emergencies/disease-outbreak-news/item/2025-DON579",
        ],
        "Bird Flu": [
            "https://www.who.int/emergencies/disease-outbreak-news/item/2025-DON590",
            "https://www.who.int/emergencies/disease-outbreak-news/item/2025-DON564",
        ],
    },
}


@retry(
    retry=retry_if_exception_type(requests.RequestException),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    stop=stop_after_attempt(3),
    reraise=True,
)
def fetch_url(session: Session, url: str, params: Optional[Dict[str, str]] = None) -> Response:
    """Fetch a URL with retry-aware error handling.

    Args:
        session: Requests session to use.
        url: URL to fetch.
        params: Optional query parameters.

    Returns:
        A successful HTTP response.
    """
    LOGGER.info("Requesting WHO URL: %s", url)
    response = session.get(url, params=params, timeout=CONFIG["timeout_seconds"])
    if response.status_code == 429:
        LOGGER.warning("WHO rate limit response received for %s", url)
    response.raise_for_status()
    return response


def save_raw_response(content: bytes, stem: str, suffix: str = ".html") -> Path:
    """Save a raw response to the configured raw data directory."""
    CONFIG["raw_dir"].mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    filename = f"who_{_slugify(stem)}_{timestamp}{suffix}"
    path = CONFIG["raw_dir"] / filename
    try:
        path.write_bytes(content)
    except OSError as exc:
        fallback_dir = Path(tempfile.gettempdir()) / "outbreak-tracker" / "data" / "raw"
        fallback_dir.mkdir(parents=True, exist_ok=True)
        path = fallback_dir / filename
        path.write_bytes(content)
        LOGGER.warning("Raw WHO archive fell back to %s after write failure: %s", path, exc)
    LOGGER.debug("Saved WHO raw response to %s", path)
    return path


def scrape_who_outbreaks(disease: Optional[str] = None) -> pd.DataFrame:
    """Scrape WHO Disease Outbreak News records.

    Args:
        disease: Optional disease name to limit the scrape.

    Returns:
        Standardized outbreak records as a DataFrame.
    """
    diseases = [disease] if disease else list(CONFIG["diseases"])
    records: List[Dict[str, object]] = []
    with requests.Session() as session:
        session.headers.update({"User-Agent": CONFIG["user_agent"]})
        for disease_name in diseases:
            try:
                listing = fetch_url(
                    session,
                    CONFIG["listing_url"],
                    params={"query": disease_name},
                )
                save_raw_response(listing.content, f"listing_{disease_name}")
                links = extract_article_links(listing.text, disease_name)
                if not links:
                    links = CONFIG["seed_article_urls"].get(disease_name, [])
                    LOGGER.info("Using %s configured WHO seed links for %s", len(links), disease_name)
                for link in links[: CONFIG["max_articles_per_disease"]]:
                    article = fetch_url(session, link)
                    save_raw_response(article.content, link.rsplit("/", 1)[-1])
                    records.append(parse_article(article.text, link, disease_name))
            except requests.RequestException as exc:
                LOGGER.error("WHO scrape failed for %s: %s", disease_name, exc)

    return pd.DataFrame(records)


def extract_article_links(html: str, disease_name: str) -> List[str]:
    """Extract Disease Outbreak News article links from a listing page."""
    soup = BeautifulSoup(html, "lxml")
    links: List[str] = []
    disease_terms = _disease_terms(disease_name)
    for anchor in soup.find_all("a", href=True):
        href = str(anchor["href"])
        text = anchor.get_text(" ", strip=True).lower()
        if "/emergencies/disease-outbreak-news/item/" not in href:
            continue
        if disease_terms and not any(term in text or term in href.lower() for term in disease_terms):
            continue
        absolute = urljoin(CONFIG["base_url"], href)
        if absolute not in links:
            links.append(absolute)
    LOGGER.info("Found %s WHO article links for %s", len(links), disease_name)
    return links


def parse_article(html: str, url: str, default_disease: str) -> Dict[str, object]:
    """Parse one WHO Disease Outbreak News article into a standard record."""
    soup = BeautifulSoup(html, "lxml")
    title = _first_text(soup, ["h1", "title"]) or default_disease
    body = soup.get_text(" ", strip=True)
    date_value = _extract_date(soup, body)
    country = _extract_country(title, body)
    counts = _extract_counts(body)
    retrieved_at = datetime.now(timezone.utc).isoformat()
    disease_name = _classify_disease(f"{title} {body}", default_disease)
    is_provisional = any(term in body.lower() for term in ("ongoing", "as of", "reported"))

    return {
        "disease": disease_name,
        "country": country,
        "date": date_value,
        "strain": _extract_strain(f"{title} {body}", disease_name),
        "confirmed_cases": counts.get("confirmed_cases"),
        "suspected_cases": counts.get("suspected_cases"),
        "deaths": counts.get("deaths"),
        "source": "WHO",
        "source_url": url,
        "retrieved_at": retrieved_at,
        "updated_at": date_value,
        "is_provisional": is_provisional,
        "notes": title,
        "ambiguous_fields": "; ".join(counts.get("ambiguous_fields", [])),
    }


def _first_text(soup: BeautifulSoup, selectors: Iterable[str]) -> Optional[str]:
    """Return the first non-empty text for a list of CSS selectors."""
    for selector in selectors:
        element = soup.select_one(selector)
        if element:
            text = element.get_text(" ", strip=True)
            if text:
                return text
    return None


def _extract_date(soup: BeautifulSoup, text: str) -> Optional[str]:
    """Extract an ISO date from article metadata or body text."""
    time_element = soup.find("time")
    if time_element:
        raw_date = time_element.get("datetime") or time_element.get_text(" ", strip=True)
        parsed = _parse_date(str(raw_date))
        if parsed:
            return parsed

    match = re.search(
        r"\b(?:\d{1,2}\s+)?(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{4}\b",
        text,
        flags=re.IGNORECASE,
    )
    return _parse_date(match.group(0)) if match else None


def _parse_date(value: str) -> Optional[str]:
    """Parse a date string into ISO format."""
    try:
        return date_parser.parse(value, fuzzy=True).date().isoformat()
    except (ValueError, OverflowError, TypeError) as exc:
        LOGGER.debug("Unable to parse WHO date %r: %s", value, exc)
        return None


def _extract_country(title: str, body: str) -> str:
    """Extract a country from a WHO title or body."""
    if "global" in title.lower():
        return "Global"
    for source in (title, body[:1200]):
        if " - " in source:
            candidate = source.split(" - ", 1)[-1].split("|", 1)[0].strip()
            if 2 <= len(candidate) <= 80:
                return candidate
        match = re.search(
            r"\b(?:Democratic Republic of the Congo|Democratic Republic of Congo|"
            r"United States of America|United States|Uganda|Guinea|Liberia|"
            r"Sierra Leone|Bangladesh|Haiti|Nigeria|Global)\b",
            source,
            flags=re.IGNORECASE,
        )
        if match:
            return match.group(0)
    return "Unknown"


def _extract_counts(text: str) -> Dict[str, object]:
    """Extract case and death counts from article text."""
    compact = re.sub(r"\s+", " ", text)
    result: Dict[str, object] = {"ambiguous_fields": []}
    patterns = {
        "confirmed_cases": [
            r"([0-9][0-9,]*)\s+(?:laboratory-)?confirmed cases",
            r"confirmed(?: human)? cases(?:[^0-9]{0,30})([0-9][0-9,]*)",
        ],
        "suspected_cases": [
            r"([0-9][0-9,]*)\s+suspected cases",
            r"suspected cases(?:[^0-9]{0,30})([0-9][0-9,]*)",
        ],
        "deaths": [
            r"([0-9][0-9,]*)\s+deaths",
            r"including\s+([0-9][0-9,]*)\s+deaths",
        ],
    }
    for field, field_patterns in patterns.items():
        matches: List[int] = []
        for pattern in field_patterns:
            for match in re.finditer(pattern, compact, flags=re.IGNORECASE):
                matches.append(int(match.group(1).replace(",", "")))
        if matches:
            result[field] = matches[0]
            if len(set(matches)) > 1:
                result["ambiguous_fields"].append(field)
        else:
            result[field] = None
            result["ambiguous_fields"].append(field)
    return result


def _classify_disease(text: str, default_disease: str) -> str:
    """Classify a disease label from article text."""
    lower_text = text.lower()
    if "mpox" in lower_text or "monkeypox" in lower_text:
        return "Mpox"
    if "cholera" in lower_text:
        return "Cholera"
    if "avian influenza" in lower_text or "bird flu" in lower_text:
        return "Bird Flu"
    if "ebola" in lower_text or "sudan virus" in lower_text:
        return "Ebola"
    return default_disease


def _extract_strain(text: str, disease_name: str) -> str:
    """Extract a strain or clade label from article text."""
    lower_text = text.lower()
    strain_patterns = [
        (r"clade\s+i[b]?", "Clade Ib"),
        (r"clade\s+ii[b]?", "Clade IIb"),
        (r"bundibugyo", "Bundibugyo"),
        (r"sudan virus|sudan ebolavirus", "Sudan"),
        (r"zaire ebolavirus|ebov|zaire", "Zaire"),
        (r"h5n1", "A(H5N1)"),
        (r"h5n5", "A(H5N5)"),
        (r"h7n9", "A(H7N9)"),
    ]
    for pattern, strain in strain_patterns:
        if re.search(pattern, lower_text):
            return strain
    return disease_name


def _disease_terms(disease_name: str) -> List[str]:
    """Return query terms used to recognize disease-specific links."""
    terms = {
        "ebola": ["ebola", "sudan-virus", "sudan virus"],
        "mpox": ["mpox", "monkeypox"],
        "cholera": ["cholera"],
        "bird flu": ["avian", "influenza", "h5n1", "h5n5", "h7n9"],
    }
    return terms.get(disease_name.lower(), [disease_name.lower()])


def _slugify(value: str) -> str:
    """Convert a string to a filesystem-friendly slug."""
    return re.sub(r"[^a-z0-9]+", "_", value.lower()).strip("_") or "response"
