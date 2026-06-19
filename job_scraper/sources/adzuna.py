"""
Adzuna source — free API key required.

Sign up at https://developer.adzuna.com (free tier is generous).
Set ADZUNA_APP_ID and ADZUNA_APP_KEY as environment variables.
"""

import os
import logging

import httpx

log = logging.getLogger(__name__)


def scrape(client: httpx.Client, cfg: dict) -> list[dict]:
    """Fetch job listings from Adzuna's search API.

    Requires ADZUNA_APP_ID and ADZUNA_APP_KEY env vars.
    Falls back to an empty list if credentials are missing.

    Args:
        client: Shared httpx session.
        cfg:    Global config dict — uses ``keywords`` to build the query.

    Returns:
        List of standardized job dicts, or [] on failure.
    """
    app_id = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        log.warning("  Adzuna skipped — ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
        return []

    log.info("Scraping Adzuna...")
    # Build a keyword query from config
    query = " OR ".join(cfg.get("keywords", [])[:5]) or "software engineer"
    country = "in"  # India — change to gb, us, au, etc. as needed

    jobs: list[dict] = []
    try:
        r = client.get(
            f"https://api.adzuna.com/v1/api/jobs/{country}/search/1",
            params={
                "app_id": app_id,
                "app_key": app_key,
                "what": query,
                "content-type": "application/json",
                "results_per_page": 50,
                "sort_by": "date",
            },
            timeout=20,
        )
        r.raise_for_status()

        for item in r.json().get("results", []):
            sal = item.get("salary_min")
            jobs.append({
                "source": "Adzuna",
                "title": item.get("title", ""),
                "company": item.get("company", {}).get("display_name", ""),
                "location": item.get("location", {}).get("display_name", ""),
                "url": item.get("redirect_url", ""),
                "tags": [item.get("category", {}).get("label", "")],
                "description": item.get("description", ""),
                "remote": "remote" in (item.get("title") or "").lower(),
                "salary_min": int(sal) if sal else None,
                "posted_at": item.get("created", ""),
            })

    except Exception as e:
        log.warning(f"  Adzuna failed: {e}")

    log.info(f"  Adzuna: {len(jobs)} listings fetched")
    return jobs
