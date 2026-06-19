"""
RemoteOK source — scrapes the public JSON API at remoteok.com/api.

No API key required.  Rate limits are generous (~1 req/s is fine).
"""

import logging

import httpx

log = logging.getLogger(__name__)


def scrape(client: httpx.Client, cfg: dict) -> list[dict]:
    """Fetch remote job listings from RemoteOK's public JSON API.

    Args:
        client: Shared httpx session with redirect support.
        cfg:    Global config dict (unused by this source but kept
                for a uniform interface).

    Returns:
        List of standardized job dicts, or [] on any failure.
    """
    log.info("Scraping RemoteOK...")
    try:
        r = client.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "JobScraper/2.0 (+github-actions)"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()

        jobs: list[dict] = []
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue
            jobs.append({
                "source": "RemoteOK",
                "title": item.get("position", ""),
                "company": item.get("company", ""),
                "location": item.get("location", "Remote"),
                "url": item.get(
                    "url",
                    f"https://remoteok.com/remote-jobs/{item.get('id', '')}",
                ),
                "tags": item.get("tags", []),
                "description": item.get("description", ""),
                "remote": True,
                "salary_min": None,
                "posted_at": item.get("date", ""),
            })

        log.info(f"  RemoteOK: {len(jobs)} listings fetched")
        return jobs

    except Exception as e:
        log.warning(f"  RemoteOK failed: {e}")
        return []
