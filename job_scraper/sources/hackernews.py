"""
Hacker News source — scrapes monthly "Who is hiring?" threads via the
HN Algolia API.

Completely free, no auth required, very generous rate limits.
Comments typically follow the format:
    Company | Role | Location | Remote | …
"""

import re
import time
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

# Location keywords used to filter comments relevant to India / remote
_DEFAULT_LOCATION_KEYWORDS = [
    "india", "bangalore", "bengaluru", "hyderabad", "mumbai",
    "pune", "delhi", "ncr", "gurgaon", "gurugram", "noida",
    "chennai", "kolkata", "ahmedabad", "remote",
]


def _parse_pipe_comment(text: str) -> dict:
    """Parse a HN 'Who is hiring?' comment that uses pipe-separated fields.

    Expected format (first line):
        Company | Role | Location | Remote | Salary | …

    Returns a dict with keys company, title, location (all may be '').
    """
    # Take only the first line
    first_line = text.split("\n")[0].strip()

    # Must have at least one pipe to be a structured comment
    if "|" not in first_line:
        return {"company": "", "title": "", "location": ""}

    parts = [p.strip() for p in first_line.split("|")]

    company = parts[0] if len(parts) > 0 else ""
    title = parts[1] if len(parts) > 1 else ""
    location = parts[2] if len(parts) > 2 else ""

    return {
        "company": company[:200],
        "title": title[:200],
        "location": location[:200],
    }


def _strip_html(text: str) -> str:
    """Remove HTML tags and decode common entities."""
    text = re.sub(r"<[^>]+>", " ", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&#x27;", "'").replace("&quot;", '"')
    return re.sub(r"\s+", " ", text).strip()


def _find_latest_thread(client: httpx.Client) -> str | None:
    """Find the objectID of the latest 'Who is hiring?' thread.

    Searches HN Algolia for story posts with 'who is hiring' in the
    last ~35 days.  Returns the objectID string, or None.
    """
    now = int(datetime.now(tz=timezone.utc).timestamp())
    thirty_five_days_ago = now - 35 * 86400

    try:
        r = client.get(
            "https://hn.algolia.com/api/v1/search_by_date",
            params={
                "query": "who is hiring",
                "tags": "story",
                "numericFilters": f"created_at_i>{thirty_five_days_ago}",
            },
            timeout=20,
        )
        r.raise_for_status()
        hits = r.json().get("hits", [])

        # Find the most recent "Ask HN: Who is hiring?" thread
        for hit in hits:
            title = (hit.get("title") or "").lower()
            if "who is hiring" in title and "ask hn" in title:
                return hit.get("objectID")

        # Fallback: return the first hit
        if hits:
            return hits[0].get("objectID")

    except Exception as e:
        log.warning(f"  HN thread search failed: {e}")

    return None


def scrape(client: httpx.Client, cfg: dict) -> list[dict]:
    """Scrape the latest HN 'Who is hiring?' thread for job postings.

    Uses the free HN Algolia API (no auth, generous limits).
    Filters comments for India-relevant or remote positions.

    Config keys:
        hn_location_keywords (list[str]): Override the default location
            filter keywords.

    Args:
        client: Shared httpx session.
        cfg:    Global config dict.

    Returns:
        List of standardized job dicts, or [] on failure.
    """
    log.info("Scraping Hacker News 'Who is hiring?'...")

    location_keywords = cfg.get("hn_location_keywords", _DEFAULT_LOCATION_KEYWORDS)
    location_keywords_lower = [kw.lower() for kw in location_keywords]

    try:
        # Step 1: Find the latest thread
        thread_id = _find_latest_thread(client)
        if not thread_id:
            log.warning("  HN: no 'Who is hiring?' thread found in the last 35 days")
            return []

        log.info(f"  HN: found thread {thread_id}, fetching comments...")
        time.sleep(0.5)  # courtesy pause

        # Step 2: Fetch top-level comments from the thread
        jobs: list[dict] = []
        page = 0
        max_pages = 5  # 5 × 100 = 500 comments max

        while page < max_pages:
            r = client.get(
                "https://hn.algolia.com/api/v1/search",
                params={
                    "tags": f"comment,story_{thread_id}",
                    "hitsPerPage": 100,
                    "page": page,
                },
                timeout=20,
            )
            r.raise_for_status()
            data = r.json()
            hits = data.get("hits", [])

            if not hits:
                break

            for hit in hits:
                raw_text = hit.get("comment_text", "")
                if not raw_text:
                    continue

                clean_text = _strip_html(raw_text)
                text_lower = clean_text.lower()

                # Filter: must mention at least one location keyword
                if not any(kw in text_lower for kw in location_keywords_lower):
                    continue

                parsed = _parse_pipe_comment(clean_text)

                # Skip if we couldn't extract a meaningful title
                if not parsed["title"] and not parsed["company"]:
                    # Use first 100 chars as title fallback
                    parsed["title"] = clean_text[:100]

                # Determine remote status
                is_remote = "remote" in text_lower

                # Timestamp
                created_at_i = hit.get("created_at_i")
                posted_at = ""
                if created_at_i:
                    posted_at = datetime.fromtimestamp(
                        created_at_i, tz=timezone.utc
                    ).isoformat()

                comment_id = hit.get("objectID", "")
                url = f"https://news.ycombinator.com/item?id={comment_id}"

                jobs.append({
                    "source": "HackerNews",
                    "title": parsed["title"],
                    "company": parsed["company"],
                    "location": parsed["location"] or ("Remote" if is_remote else ""),
                    "url": url,
                    "tags": ["hn-who-is-hiring"],
                    "description": clean_text[:3000],
                    "remote": is_remote,
                    "salary_min": None,
                    "posted_at": posted_at,
                })

            page += 1
            time.sleep(0.5)  # courtesy pause between pages

    except Exception as e:
        log.warning(f"  Hacker News scraping failed: {e}")
        return []

    log.info(f"  Hacker News: {len(jobs)} job posts collected")
    return jobs
