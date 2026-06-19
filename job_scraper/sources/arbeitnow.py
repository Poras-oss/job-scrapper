"""
Arbeitnow source — free public JSON API at arbeitnow.com.

No API key required.  Paginates up to 3 pages (~75 listings).
"""

import logging

import httpx

log = logging.getLogger(__name__)


def scrape(client: httpx.Client, cfg: dict) -> list[dict]:
    """Fetch listings from Arbeitnow's public JSON API.

    Args:
        client: Shared httpx session.
        cfg:    Global config dict (unused by this source).

    Returns:
        List of standardized job dicts, or [] on failure.
    """
    log.info("Scraping Arbeitnow...")
    jobs: list[dict] = []
    page = 1

    try:
        while page <= 3:  # cap at 3 pages to stay polite
            r = client.get(
                "https://www.arbeitnow.com/api/job-board-api",
                params={"page": page},
                timeout=20,
            )
            r.raise_for_status()
            data = r.json().get("data", [])
            if not data:
                break

            for item in data:
                jobs.append({
                    "source": "Arbeitnow",
                    "title": item.get("title", ""),
                    "company": item.get("company_name", ""),
                    "location": item.get("location", ""),
                    "url": item.get("url", ""),
                    "tags": item.get("tags", []),
                    "description": item.get("description", ""),
                    "remote": item.get("remote", False),
                    "salary_min": None,
                    "posted_at": item.get("created_at", ""),
                })
            page += 1

    except Exception as e:
        log.warning(f"  Arbeitnow failed: {e}")

    log.info(f"  Arbeitnow: {len(jobs)} listings fetched")
    return jobs
