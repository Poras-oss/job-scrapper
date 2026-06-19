"""
WeWorkRemotely source — scrapes public RSS feeds from weworkremotely.com.

No API key required.  Multiple category feeds are consumed.
"""

import logging
import xml.etree.ElementTree as ET

import httpx

log = logging.getLogger(__name__)

# RSS feed URLs and their category labels
_FEEDS: list[tuple[str, str]] = [
    ("https://weworkremotely.com/categories/remote-programming-jobs.rss", "Programming"),
    ("https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss", "DevOps"),
    ("https://weworkremotely.com/categories/remote-design-jobs.rss", "Design"),
    ("https://weworkremotely.com/remote-jobs.rss", "All"),
]


def scrape(client: httpx.Client, cfg: dict) -> list[dict]:
    """Fetch remote listings from WeWorkRemotely RSS feeds.

    Args:
        client: Shared httpx session.
        cfg:    Global config dict (unused by this source).

    Returns:
        List of standardized job dicts.
    """
    log.info("Scraping WeWorkRemotely...")
    jobs: list[dict] = []

    for url, category in _FEEDS:
        try:
            r = client.get(url, timeout=20)
            r.raise_for_status()
            root = ET.fromstring(r.content)

            for item in root.findall(".//item"):
                def _tag(name: str, _item=item) -> str:
                    """Extract text from an XML child element."""
                    el = _item.find(name)
                    return el.text.strip() if el is not None and el.text else ""

                # Titles are typically "Company: Job Title"
                title_full = _tag("title")
                parts = title_full.split(":", 1)
                company = parts[0].strip() if len(parts) > 1 else ""
                title = parts[1].strip() if len(parts) > 1 else title_full

                jobs.append({
                    "source": "WeWorkRemotely",
                    "title": title,
                    "company": company,
                    "location": "Remote",
                    "url": _tag("link") or _tag("guid"),
                    "tags": [category.lower()],
                    "description": _tag("description"),
                    "remote": True,
                    "salary_min": None,
                    "posted_at": _tag("pubDate"),
                })

        except Exception as e:
            log.warning(f"  WWR feed {url} failed: {e}")

    log.info(f"  WeWorkRemotely: {len(jobs)} listings fetched")
    return jobs
