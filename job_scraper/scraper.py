"""
Job Scraper + Telegram Notifier
Reads preferences from config.yaml, scrapes job boards with public APIs/RSS,
deduplicates via seen_jobs.json, and sends Telegram messages for new matches.
"""

import os
import json
import hashlib
import logging
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path

import httpx
import yaml

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
CONFIG_PATH = BASE_DIR / "config.yaml"
SEEN_JOBS_PATH = BASE_DIR / "seen_jobs.json"

# ── Config ────────────────────────────────────────────────────────────────────
def load_config() -> dict:
    with open(CONFIG_PATH) as f:
        return yaml.safe_load(f)

# ── Dedup cache ───────────────────────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_JOBS_PATH.exists():
        with open(SEEN_JOBS_PATH) as f:
            return set(json.load(f))
    return set()

def save_seen(seen: set):
    # Keep only last 5000 IDs to avoid unbounded growth
    ids = list(seen)[-5000:]
    with open(SEEN_JOBS_PATH, "w") as f:
        json.dump(ids, f)

def job_id(job: dict) -> str:
    """Stable hash from URL or title+company."""
    key = job.get("url") or f"{job.get('title','')}|{job.get('company','')}"
    return hashlib.sha1(key.encode()).hexdigest()

# ── Matching logic ────────────────────────────────────────────────────────────
def matches(job: dict, cfg: dict) -> bool:
    title = (job.get("title") or "").lower()
    desc  = (job.get("description") or "").lower()
    tags  = [t.lower() for t in (job.get("tags") or [])]
    loc   = (job.get("location") or "").lower()
    salary = job.get("salary_min") or 0

    combined = f"{title} {desc} {' '.join(tags)}"

    # Keyword filter (OR)
    keywords = [k.lower() for k in cfg.get("keywords", [])]
    if keywords and not any(kw in combined for kw in keywords):
        return False

    # Role type filter
    role_types = [r.lower() for r in cfg.get("role_types", [])]
    if role_types and not any(rt in title for rt in role_types):
        return False

    # Remote filter
    if cfg.get("remote_only"):
        is_remote = (
            job.get("remote") is True
            or "remote" in loc
            or "remote" in title
            or "remote" in desc[:200]
        )
        if not is_remote:
            return False

    # Location keyword filter (when not remote_only)
    loc_keywords = [l.lower() for l in cfg.get("location_keywords", [])]
    if not cfg.get("remote_only") and loc_keywords:
        if not any(lk in loc for lk in loc_keywords):
            return False

    # Experience level filter
    exp_levels = [e.lower() for e in cfg.get("experience_levels", [])]
    if exp_levels and "any" not in exp_levels:
        if not any(el in title or el in desc[:300] for el in exp_levels):
            return False

    # Salary filter
    min_sal = cfg.get("min_salary", 0)
    if min_sal and salary and salary < min_sal:
        return False

    # Tag filter (RemoteOK)
    cfg_tags = [t.lower() for t in cfg.get("tags", [])]
    if cfg_tags and tags:
        if not any(ct in tags for ct in cfg_tags):
            return False

    return True

# ── Scrapers ──────────────────────────────────────────────────────────────────

def scrape_remoteok(client: httpx.Client) -> list[dict]:
    """RemoteOK public JSON API — no key required."""
    log.info("Scraping RemoteOK...")
    try:
        r = client.get(
            "https://remoteok.com/api",
            headers={"User-Agent": "JobScraper/1.0 (+github-actions)"},
            timeout=20,
        )
        r.raise_for_status()
        data = r.json()
        jobs = []
        for item in data:
            if not isinstance(item, dict) or not item.get("position"):
                continue
            jobs.append({
                "source": "RemoteOK",
                "title": item.get("position", ""),
                "company": item.get("company", ""),
                "location": item.get("location", "Remote"),
                "url": item.get("url", f"https://remoteok.com/remote-jobs/{item.get('id','')}"),
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


def scrape_weworkremotely(client: httpx.Client) -> list[dict]:
    """We Work Remotely — public RSS feeds."""
    log.info("Scraping WeWorkRemotely...")
    feeds = [
        ("https://weworkremotely.com/categories/remote-programming-jobs.rss", "Programming"),
        ("https://weworkremotely.com/categories/remote-devops-sysadmin-jobs.rss", "DevOps"),
        ("https://weworkremotely.com/categories/remote-design-jobs.rss", "Design"),
        ("https://weworkremotely.com/remote-jobs.rss", "All"),
    ]
    jobs = []
    for url, category in feeds:
        try:
            r = client.get(url, timeout=20)
            r.raise_for_status()
            root = ET.fromstring(r.content)
            for item in root.findall(".//item"):
                def tag(name):
                    el = item.find(name)
                    return el.text.strip() if el is not None and el.text else ""

                title_full = tag("title")  # "Company: Job Title"
                parts = title_full.split(":", 1)
                company = parts[0].strip() if len(parts) > 1 else ""
                title   = parts[1].strip() if len(parts) > 1 else title_full

                jobs.append({
                    "source": "WeWorkRemotely",
                    "title": title,
                    "company": company,
                    "location": "Remote",
                    "url": tag("link") or tag("guid"),
                    "tags": [category.lower()],
                    "description": tag("description"),
                    "remote": True,
                    "salary_min": None,
                    "posted_at": tag("pubDate"),
                })
        except Exception as e:
            log.warning(f"  WWR feed {url} failed: {e}")
    log.info(f"  WeWorkRemotely: {len(jobs)} listings fetched")
    return jobs


def scrape_arbeitnow(client: httpx.Client) -> list[dict]:
    """Arbeitnow — free public JSON API, no key required."""
    log.info("Scraping Arbeitnow...")
    jobs = []
    page = 1
    try:
        while page <= 3:  # cap at 3 pages = ~75 jobs
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


def scrape_adzuna(client: httpx.Client, cfg: dict) -> list[dict]:
    """
    Adzuna — free API key required.
    Sign up at https://developer.adzuna.com (free, generous limits).
    Set ADZUNA_APP_ID and ADZUNA_APP_KEY in GitHub Secrets.
    """
    app_id  = os.getenv("ADZUNA_APP_ID")
    app_key = os.getenv("ADZUNA_APP_KEY")
    if not app_id or not app_key:
        log.warning("  Adzuna skipped — ADZUNA_APP_ID / ADZUNA_APP_KEY not set")
        return []

    log.info("Scraping Adzuna...")
    # Build a keyword query from config
    query = " OR ".join(cfg.get("keywords", [])[:5]) or "software engineer"
    country = "in"  # change to gb, us, au, etc.

    jobs = []
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

# ── Telegram ──────────────────────────────────────────────────────────────────

def format_message(job: dict, cfg: dict) -> str:
    """Build a nicely formatted Telegram message (MarkdownV2)."""
    def esc(text: str) -> str:
        """Escape special chars for Telegram MarkdownV2."""
        for ch in r"\_*[]()~`>#+-=|{}.!":
            text = text.replace(ch, f"\\{ch}")
        return text

    title   = esc(job.get("title", "Unknown Role"))
    company = esc(job.get("company", "Unknown Company"))
    loc     = esc(job.get("location", "Remote"))
    source  = esc(job.get("source", ""))
    url     = job.get("url", "")
    tags    = job.get("tags", [])

    tags_str = " ".join(f"\\#{esc(t.replace(' ', '_'))}" for t in tags[:5]) if tags else ""

    lines = [
        f"*{title}*",
        f"🏢 {company}",
        f"📍 {loc}",
    ]

    if job.get("salary_min"):
        sal = esc(f"${job['salary_min']:,}+")
        lines.append(f"💰 {sal}")

    if cfg.get("include_description_snippet") and job.get("description"):
        snip_len = cfg.get("description_snippet_length", 280)
        raw_desc = job["description"]
        # Strip basic HTML tags
        import re
        clean = re.sub(r"<[^>]+>", " ", raw_desc)
        clean = re.sub(r"\s+", " ", clean).strip()[:snip_len]
        if len(raw_desc) > snip_len:
            clean += "…"
        lines.append(f"\n_{esc(clean)}_")

    if tags_str:
        lines.append(f"\n{tags_str}")

    lines.append(f"\n[View Job →]({url})")
    lines.append(f"_via {source}_")

    return "\n".join(lines)


def send_telegram(message: str, token: str, chat_id: str, client: httpx.Client):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": message,
        "parse_mode": "MarkdownV2",
        "disable_web_page_preview": False,
    }
    try:
        r = client.post(url, json=payload, timeout=15)
        r.raise_for_status()
    except httpx.HTTPStatusError as e:
        # Log response body for debugging (e.g. bad escape in message)
        log.error(f"Telegram API error: {e.response.status_code} — {e.response.text[:300]}")
    except Exception as e:
        log.error(f"Telegram send failed: {e}")


def send_summary(count: int, token: str, chat_id: str, client: httpx.Client):
    ts = datetime.utcnow().strftime("%Y\\-%-m\\-%-d %H:%M UTC")
    msg = f"✅ *Job Scraper* — {ts}\n_{count} new job{'s' if count != 1 else ''} found and sent\\._"
    send_telegram(msg, token, chat_id, client)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    seen = load_seen()

    token   = os.environ.get("TELEGRAM_TOKEN")
    chat_id = os.environ.get("TELEGRAM_CHAT_ID")

    if not token or not chat_id:
        log.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — aborting")
        return

    sources_cfg = cfg.get("sources", {})
    max_notif = cfg.get("max_notifications_per_run", 10)

    all_jobs: list[dict] = []

    with httpx.Client(follow_redirects=True) as client:
        if sources_cfg.get("remoteok"):
            all_jobs.extend(scrape_remoteok(client))
        if sources_cfg.get("weworkremotely"):
            all_jobs.extend(scrape_weworkremotely(client))
        if sources_cfg.get("arbeitnow"):
            all_jobs.extend(scrape_arbeitnow(client))
        if sources_cfg.get("adzuna"):
            all_jobs.extend(scrape_adzuna(client, cfg))

        log.info(f"Total listings fetched: {len(all_jobs)}")

        new_matches: list[dict] = []
        for job in all_jobs:
            jid = job_id(job)
            if jid in seen:
                continue
            if matches(job, cfg):
                new_matches.append(job)
                seen.add(jid)
            else:
                # Still mark as seen so we don't re-evaluate next run
                seen.add(jid)

        log.info(f"New matching jobs: {len(new_matches)}")

        # Cap to avoid flooding
        capped = new_matches[:max_notif]
        if len(new_matches) > max_notif:
            log.info(f"Capped at {max_notif} notifications (config: max_notifications_per_run)")

        for job in capped:
            msg = format_message(job, cfg)
            send_telegram(msg, token, chat_id, client)
            log.info(f"  Sent: [{job['source']}] {job['title']} @ {job['company']}")

        if capped:
            send_summary(len(capped), token, chat_id, client)
        elif cfg.get("notify_on_no_results"):
            ts = datetime.utcnow().strftime("%Y\\-%-m\\-%-d %H:%M UTC")
            send_telegram(
                f"🔍 *Job Scraper* — {ts}\n_No new matching jobs this run\\._",
                token, chat_id, client
            )

    save_seen(seen)
    log.info("Done.")


if __name__ == "__main__":
    main()
