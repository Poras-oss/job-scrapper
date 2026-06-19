"""
Job Scraper + Telegram Notifier
Reads preferences from config.yaml, scrapes job boards with public APIs/RSS,
deduplicates via seen_jobs.json, and sends Telegram messages for new matches.
"""

import os
import re
import json
import time
import hashlib
import logging
from datetime import datetime, timezone, timedelta
from pathlib import Path

import httpx
import yaml

# ── Source imports ─────────────────────────────────────────────────────────────
from .sources import (
    scrape_remoteok,
    scrape_weworkremotely,
    scrape_arbeitnow,
    scrape_adzuna,
    scrape_reddit,
    scrape_google_jobs,
    scrape_hackernews,
    scrape_jobspy,
)

# ── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger(__name__)

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR = Path(__file__).parent
ROOT_DIR = BASE_DIR.parent
CONFIG_PATH = ROOT_DIR / "config.yaml"
SEEN_JOBS_PATH = ROOT_DIR / "seen_jobs.json"
LAST_RUN_PATH = ROOT_DIR / "last_run.json"

# ── Source Registry ───────────────────────────────────────────────────────────
# Maps source name → scraping function and default run frequency.
# Frequencies can be overridden from config.yaml.
SOURCES = {
    "remoteok":        {"fn": scrape_remoteok,        "frequency": "every_run"},
    "weworkremotely":  {"fn": scrape_weworkremotely,  "frequency": "every_run"},
    "arbeitnow":       {"fn": scrape_arbeitnow,       "frequency": "every_run"},
    "adzuna":          {"fn": scrape_adzuna,           "frequency": "every_8h"},
    "reddit":          {"fn": scrape_reddit,           "frequency": "every_run"},
    "google_jobs":     {"fn": scrape_google_jobs,      "frequency": "daily"},
    "hackernews":      {"fn": scrape_hackernews,       "frequency": "every_run"},
    "jobspy":          {"fn": scrape_jobspy,            "frequency": "daily"},
}

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

# ── Last-run tracking (smart scheduling) ─────────────────────────────────────

def load_last_runs() -> dict:
    """Load last-run timestamps for each source from last_run.json."""
    if LAST_RUN_PATH.exists():
        try:
            with open(LAST_RUN_PATH) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            log.warning(f"Could not load last_run.json, starting fresh: {e}")
    return {}

def save_last_runs(last_runs: dict):
    """Persist last-run timestamps to last_run.json."""
    with open(LAST_RUN_PATH, "w") as f:
        json.dump(last_runs, f, indent=2)

def should_run(source_name: str, frequency: str, last_runs: dict) -> bool:
    """Check if enough time has passed since last run for this source."""
    intervals = {"every_run": 0, "every_8h": 8 * 3600, "every_12h": 12 * 3600, "daily": 23 * 3600}
    min_interval = intervals.get(frequency, 0)
    last = last_runs.get(source_name)
    if not last:
        return True
    elapsed = time.time() - last
    return elapsed >= min_interval

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

# ── Telegram ──────────────────────────────────────────────────────────────────

def esc(text: str) -> str:
    """Escape special chars for Telegram MarkdownV2."""
    for ch in r"\_*[]()~`>#+-=|{}.!":
        text = text.replace(ch, f"\\{ch}")
    return text


def format_single_message(job: dict, cfg: dict) -> str:
    """Build a nicely formatted Telegram message (MarkdownV2) for a single job.

    Kept for backward compatibility — previously named format_message().
    """
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


# Backward-compatible alias
format_message = format_single_message


def format_digest(jobs: list[dict], cfg: dict) -> list[str]:
    """Format jobs into digest-style Telegram messages (MarkdownV2).

    Groups up to max_jobs_per_digest jobs per message.
    Returns a list of message strings, each under the 4096-char Telegram limit.
    """
    max_per_digest = cfg.get("max_jobs_per_digest", 10)
    ist = timezone(timedelta(hours=5, minutes=30))
    now_ist = datetime.now(ist).strftime("%Y\\-%m\\-%d %H:%M IST")

    messages = []
    chunks = [jobs[i:i + max_per_digest] for i in range(0, len(jobs), max_per_digest)]

    for chunk_idx, chunk in enumerate(chunks):
        total_label = len(jobs) if len(chunks) == 1 else f"{len(jobs)} total, part {chunk_idx + 1}"
        header = (
            f"🔔 *Job Alert — {esc(str(len(chunk)))} New Matches*\n"
            f"📅 {now_ist}\n"
        )

        entries = []
        number_emojis = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣", "6️⃣", "7️⃣", "8️⃣", "9️⃣", "🔟"]
        for i, job in enumerate(chunk):
            emoji = number_emojis[i] if i < len(number_emojis) else f"*{i + 1}\\.*"
            title   = esc(job.get("title", "Unknown Role"))
            company = esc(job.get("company", "Unknown Company"))
            loc     = esc(job.get("location", "Remote"))
            source  = esc(job.get("source", ""))
            url     = job.get("url", "")

            entry_lines = [
                "━━━━━━━━━━━━━━━━━━━",
                f"{emoji} *{title}*",
                f"🏢 {company} \\| 📍 {loc}",
            ]

            if job.get("salary_min"):
                sal = esc(f"${job['salary_min']:,}+")
                entry_lines.append(f"💰 {sal}")

            entry_lines.append(f"🔗 [Apply →]({url})")
            entry_lines.append(f"_via {source}_")

            entries.append("\n".join(entry_lines))

        full_msg = header + "\n" + "\n\n".join(entries)

        # Split further if the message exceeds Telegram's 4096 char limit
        if len(full_msg) <= 4096:
            messages.append(full_msg)
        else:
            # Fallback: send entries individually with a mini-header
            for entry in entries:
                mini_msg = header + "\n" + entry
                if len(mini_msg) > 4096:
                    # Truncate if a single entry is somehow too long
                    mini_msg = mini_msg[:4090] + "\\.\\.\\."
                messages.append(mini_msg)

    return messages


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
    ist = timezone(timedelta(hours=5, minutes=30))
    ts = datetime.now(ist).strftime("%Y\\-%m\\-%d %H:%M IST")
    msg = f"✅ *Job Scraper* — {ts}\n_{count} new job{'s' if count != 1 else ''} found and sent\\._"
    send_telegram(msg, token, chat_id, client)


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    cfg = load_config()
    seen = load_seen()
    last_runs = load_last_runs()

    token   = (os.environ.get("TELEGRAM_TOKEN") or "").strip()
    chat_id = (os.environ.get("TELEGRAM_CHAT_ID") or "").strip()

    if not token or not chat_id:
        log.error("TELEGRAM_TOKEN or TELEGRAM_CHAT_ID not set — aborting")
        return

    sources_cfg = cfg.get("sources", {})
    max_notif = cfg.get("max_notifications_per_run", 10)
    notification_style = cfg.get("notification_style", "digest")

    all_jobs: list[dict] = []

    with httpx.Client(follow_redirects=True) as client:
        # ── Scrape each enabled + due source ──────────────────────────────
        for name, registry_entry in SOURCES.items():
            src_cfg = sources_cfg.get(name, {})

            # Handle both old format (bool) and new format (dict with enabled/frequency)
            if isinstance(src_cfg, bool):
                enabled = src_cfg
                frequency = registry_entry["frequency"]
            elif isinstance(src_cfg, dict):
                enabled = src_cfg.get("enabled", False)
                frequency = src_cfg.get("frequency", registry_entry["frequency"])
            else:
                enabled = False
                frequency = registry_entry["frequency"]

            if not enabled:
                log.info(f"  ⏭ {name}: disabled in config")
                continue

            if not should_run(name, frequency, last_runs):
                log.info(f"  ⏭ {name}: skipped (frequency={frequency}, not due yet)")
                continue

            log.info(f"  ▶ Running source: {name} (frequency={frequency})")
            try:
                scrape_fn = registry_entry["fn"]
                jobs = scrape_fn(client, cfg)
                all_jobs.extend(jobs)
                # Update last-run timestamp on success
                last_runs[name] = time.time()
                log.info(f"  ✓ {name}: {len(jobs)} listings fetched")
            except Exception as e:
                log.error(f"  ✗ {name} failed: {e}")

        log.info(f"Total listings fetched: {len(all_jobs)}")

        # ── Filter and dedup ──────────────────────────────────────────────
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

        # ── Send notifications ────────────────────────────────────────────
        if capped:
            if notification_style == "digest":
                digest_msgs = format_digest(capped, cfg)
                for msg in digest_msgs:
                    send_telegram(msg, token, chat_id, client)
                log.info(f"  Sent digest: {len(capped)} jobs in {len(digest_msgs)} message(s)")
            else:
                # Individual messages (legacy behavior)
                for job in capped:
                    msg = format_single_message(job, cfg)
                    send_telegram(msg, token, chat_id, client)
                    log.info(f"  Sent: [{job['source']}] {job['title']} @ {job['company']}")

            send_summary(len(capped), token, chat_id, client)
        elif cfg.get("notify_on_no_results"):
            ist = timezone(timedelta(hours=5, minutes=30))
            ts = datetime.now(ist).strftime("%Y\\-%m\\-%d %H:%M IST")
            send_telegram(
                f"🔍 *Job Scraper* — {ts}\n_No new matching jobs this run\\._",
                token, chat_id, client,
            )

    # ── Persist state ─────────────────────────────────────────────────────
    save_seen(seen)
    save_last_runs(last_runs)
    log.info("Done.")


if __name__ == "__main__":
    main()
