"""
Google Jobs source — uses SerpApi to query Google Jobs results for India.

Requires a SERPAPI_KEY environment variable.
Free tier: 100 searches/month — we conserve by making only 1 API call
per run and rotating queries by day-of-month.
"""

import os
import re
import logging
from datetime import datetime

import httpx

log = logging.getLogger(__name__)

_DEFAULT_QUERIES = [
    "software engineer India",
    "python developer India",
]


def _parse_salary(extensions: dict) -> int | None:
    """Extract a numeric salary floor from SerpApi detected_extensions.

    The ``salary`` field may look like ``"₹8L–₹12L"`` or ``"$80K–$120K"``.
    We try to grab the first number and interpret suffixes (L = lakh, K = thousand).
    """
    raw = extensions.get("salary", "") if extensions else ""
    if not raw:
        return None

    # Grab the first number (possibly with decimals)
    m = re.search(r"([\d,.]+)\s*([LlKk])?", raw)
    if not m:
        return None

    num = float(m.group(1).replace(",", ""))
    suffix = (m.group(2) or "").upper()
    if suffix == "L":
        return int(num * 100_000)
    if suffix == "K":
        return int(num * 1_000)
    return int(num)


def scrape(client: httpx.Client, cfg: dict) -> list[dict]:
    """Fetch Google Jobs results via SerpApi.

    Only 1 API call is made per run to conserve the free-tier quota.
    The query is rotated daily using ``day_of_month % len(queries)``.

    Config keys:
        google_jobs_queries (list[str]): Custom query strings to rotate through.

    Env vars:
        SERPAPI_KEY: SerpApi API key.

    Args:
        client: Shared httpx session.
        cfg:    Global config dict.

    Returns:
        List of standardized job dicts, or [] if no API key / on failure.
    """
    api_key = os.getenv("SERPAPI_KEY")
    if not api_key:
        log.warning("  Google Jobs skipped — SERPAPI_KEY not set")
        return []

    queries = cfg.get("google_jobs_queries", _DEFAULT_QUERIES)
    if not queries:
        queries = _DEFAULT_QUERIES

    # Rotate through queries by day-of-month to spread API usage
    query_index = datetime.utcnow().day % len(queries)
    query = queries[query_index]

    log.info(f"Scraping Google Jobs via SerpApi (query: '{query}')...")

    jobs: list[dict] = []
    try:
        r = client.get(
            "https://serpapi.com/search.json",
            params={
                "engine": "google_jobs",
                "q": query,
                "location": "India",
                "api_key": api_key,
            },
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()

        for item in data.get("jobs_results", []):
            extensions = item.get("detected_extensions", {})
            salary_min = _parse_salary(extensions)

            # Prefer apply_link over share_link
            apply_options = item.get("apply_options", [])
            url = ""
            if apply_options and isinstance(apply_options, list):
                url = apply_options[0].get("link", "")
            if not url:
                url = item.get("share_link", item.get("link", ""))

            # Determine remote
            location = item.get("location", "")
            title = item.get("title", "")
            schedule = (extensions.get("schedule_type") or "").lower()
            is_remote = "remote" in f"{location} {title} {schedule}".lower()

            # Tags from schedule type / qualifications
            tags: list[str] = []
            if schedule:
                tags.append(schedule)

            jobs.append({
                "source": "GoogleJobs",
                "title": title,
                "company": item.get("company_name", ""),
                "location": location,
                "url": url,
                "tags": tags,
                "description": item.get("description", "")[:3000],
                "remote": is_remote,
                "salary_min": salary_min,
                "posted_at": item.get("detected_extensions", {}).get(
                    "posted_at", ""
                ),
            })

    except Exception as e:
        log.warning(f"  Google Jobs failed: {e}")

    log.info(f"  Google Jobs: {len(jobs)} listings fetched")
    return jobs
