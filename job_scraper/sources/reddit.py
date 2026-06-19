"""
Reddit source — scrapes India-focused job subreddits via Reddit's public JSON API.

Subreddits scraped by default:
  r/developersIndia, r/ITjobsinindia, r/IndiaCareers, r/Indiajobs

No authentication required (uses the unauthenticated JSON endpoints).
Rate limit: ~60 requests/min for unauthenticated clients.
"""

import re
import time
import logging
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

# ── Defaults ────────────────────────────────────────────────────────────────
_DEFAULT_SUBREDDITS = [
    "developersIndia",
    "ITjobsinindia",
    "IndiaCareers",
    "Indiajobs",
]

_USER_AGENT = "JobScraper/2.0 (by /u/jobbot)"

# Simple regex patterns for extracting company / location from post text
_COMPANY_RE = re.compile(
    r"(?:company|org(?:anization)?|employer)\s*[:：\-–—]\s*(.+)",
    re.IGNORECASE,
)
_LOCATION_RE = re.compile(
    r"(?:location|city|place|office)\s*[:：\-–—]\s*(.+)",
    re.IGNORECASE,
)

# Broader pattern: "at <Company>" or "@ <Company>"
_AT_COMPANY_RE = re.compile(
    r"(?:hiring\s+at|@)\s+([A-Z][\w &.\-]+)",
    re.IGNORECASE,
)


def _extract_field(pattern: re.Pattern, text: str) -> str:
    """Return the first capture group from *pattern* in *text*, or ''."""
    m = pattern.search(text)
    return m.group(1).strip() if m else ""


def _extract_company(title: str, body: str) -> str:
    """Try several heuristics to pull a company name from a Reddit post."""
    # Explicit "Company: …" line
    for text in (body, title):
        val = _extract_field(_COMPANY_RE, text)
        if val:
            return val[:120]

    # "hiring at <Company>" / "@ <Company>"
    for text in (title, body):
        val = _extract_field(_AT_COMPANY_RE, text)
        if val:
            return val[:120]

    return ""


def _extract_location(title: str, body: str) -> str:
    """Try to pull a location string from title or body."""
    for text in (body, title):
        val = _extract_field(_LOCATION_RE, text)
        if val:
            return val[:120]
    return ""


def _is_job_post(title: str, body: str) -> bool:
    """Rough heuristic: does this post look like a job listing?"""
    combined = f"{title} {body}".lower()
    job_signals = [
        "hiring", "job", "opening", "referral", "opportunity",
        "position", "vacancy", "looking for", "apply", "jd",
        "work from home", "wfh", "remote",
    ]
    return any(kw in combined for kw in job_signals)


def _fetch_subreddit(
    client: httpx.Client,
    subreddit: str,
    headers: dict,
) -> list[dict]:
    """Fetch posts from a single subreddit via search + new endpoints.

    Returns raw Reddit post dicts (from ``data.children[].data``).
    """
    raw_posts: dict[str, dict] = {}  # keyed by post id to deduplicate

    # 1. Search for job-related posts
    search_url = f"https://www.reddit.com/r/{subreddit}/search.json"
    search_params = {
        "q": "hiring OR job OR referral OR openings",
        "restrict_sr": "on",
        "sort": "new",
        "limit": 25,
    }
    try:
        r = client.get(search_url, params=search_params, headers=headers, timeout=20)
        r.raise_for_status()
        for child in r.json().get("data", {}).get("children", []):
            post = child.get("data", {})
            raw_posts[post.get("id", "")] = post
    except Exception as e:
        log.warning(f"  Reddit search r/{subreddit} failed: {e}")

    # Small delay to respect rate limits
    time.sleep(1.0)

    # 2. Latest posts (catches posts that don't match the search keywords)
    new_url = f"https://www.reddit.com/r/{subreddit}/new.json"
    try:
        r = client.get(new_url, params={"limit": 25}, headers=headers, timeout=20)
        r.raise_for_status()
        for child in r.json().get("data", {}).get("children", []):
            post = child.get("data", {})
            raw_posts[post.get("id", "")] = post
    except Exception as e:
        log.warning(f"  Reddit new r/{subreddit} failed: {e}")

    return list(raw_posts.values())


def scrape(client: httpx.Client, cfg: dict) -> list[dict]:
    """Scrape Indian job subreddits for job/referral posts.

    Uses Reddit's public (unauthenticated) JSON API.
    Rate limit: ~60 req/min — we add short sleeps between subreddits.

    Config keys:
        reddit_subreddits (list[str]): Override default subreddit list.

    Args:
        client: Shared httpx session.
        cfg:    Global config dict.

    Returns:
        List of standardized job dicts.
    """
    subreddits = cfg.get("reddit_subreddits", _DEFAULT_SUBREDDITS)
    headers = {"User-Agent": _USER_AGENT}
    log.info(f"Scraping Reddit ({len(subreddits)} subreddits)...")

    jobs: list[dict] = []
    try:
        for idx, subreddit in enumerate(subreddits):
            raw_posts = _fetch_subreddit(client, subreddit, headers)

            for post in raw_posts:
                title = post.get("title", "")
                body = post.get("selftext", "")

                if not _is_job_post(title, body):
                    continue

                # Normalise timestamp
                created_utc = post.get("created_utc")
                posted_at = ""
                if created_utc:
                    posted_at = datetime.fromtimestamp(
                        created_utc, tz=timezone.utc
                    ).isoformat()

                company = _extract_company(title, body)
                location = _extract_location(title, body)

                # Determine remote status
                combined_lower = f"{title} {body}".lower()
                is_remote = any(
                    kw in combined_lower
                    for kw in ("remote", "wfh", "work from home")
                )

                permalink = post.get("permalink", "")
                url = f"https://www.reddit.com{permalink}" if permalink else post.get("url", "")

                jobs.append({
                    "source": "Reddit",
                    "title": title[:200],
                    "company": company,
                    "location": location or ("Remote" if is_remote else "India"),
                    "url": url,
                    "tags": [f"r/{subreddit}"],
                    "description": body[:2000],
                    "remote": is_remote,
                    "salary_min": None,
                    "posted_at": posted_at,
                })

            # Rate-limit courtesy pause between subreddits
            if idx < len(subreddits) - 1:
                time.sleep(1.5)

    except Exception as e:
        log.warning(f"  Reddit scraping failed: {e}")

    log.info(f"  Reddit: {len(jobs)} job posts collected")
    return jobs
