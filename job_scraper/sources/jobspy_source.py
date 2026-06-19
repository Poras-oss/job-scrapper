"""
JobSpy source — wrapper around the ``python-jobspy`` library.

This module delegates actual scraping to the ``jobspy`` package, which
handles Indeed, LinkedIn, Glassdoor, and others under the hood.
If the library is not installed the module degrades gracefully.

Install:  pip install python-jobspy
"""

import logging

import httpx  # imported for type signature consistency; not used directly

log = logging.getLogger(__name__)

_DEFAULT_SITES = ["indeed", "linkedin", "glassdoor"]


def scrape(client: httpx.Client, cfg: dict) -> list[dict]:
    """Scrape multiple job boards via python-jobspy.

    ``python-jobspy`` manages its own HTTP sessions, so ``client`` is
    accepted for interface consistency but not passed through.

    Config keys:
        jobspy_sites (list[str]):  Sites to scrape (default: indeed,
            linkedin, glassdoor).
        keywords (list[str]):      Used to build the search term.

    Args:
        client: Shared httpx session (unused — jobspy manages its own).
        cfg:    Global config dict.

    Returns:
        List of standardized job dicts, or [] if jobspy is unavailable.
    """
    # Graceful import — jobspy is an optional dependency
    try:
        from jobspy import scrape_jobs
    except ImportError:
        log.warning(
            "  JobSpy skipped — python-jobspy not installed. "
            "Install with: pip install python-jobspy"
        )
        return []

    sites = cfg.get("jobspy_sites", _DEFAULT_SITES)
    keywords = cfg.get("keywords", [])
    search_term = " ".join(keywords[:5]) if keywords else "software engineer"

    log.info(f"Scraping via JobSpy (sites={sites}, query='{search_term}')...")

    jobs: list[dict] = []
    try:
        df = scrape_jobs(
            site_name=sites,
            search_term=search_term,
            location="India",
            results_wanted=25,
            country_indeed="India",
        )

        if df is None or df.empty:
            log.info("  JobSpy: no results returned")
            return []

        for _, row in df.iterrows():
            # Determine remote status
            location = str(row.get("location", ""))
            title = str(row.get("title", ""))
            is_remote = any(
                kw in f"{location} {title}".lower()
                for kw in ("remote", "wfh", "work from home")
            )

            # Parse salary — jobspy may return min_amount or interval
            salary_min = None
            raw_sal = row.get("min_amount")
            if raw_sal is not None:
                try:
                    salary_min = int(float(raw_sal))
                except (ValueError, TypeError):
                    pass

            # Build URL
            url = str(row.get("job_url", row.get("link", "")))

            # Build tags from site name and job type
            tags: list[str] = []
            site = str(row.get("site", ""))
            if site:
                tags.append(site)
            job_type = str(row.get("job_type", ""))
            if job_type and job_type != "nan":
                tags.append(job_type)

            # Posted date
            posted_at = str(row.get("date_posted", ""))
            if posted_at == "NaT" or posted_at == "nan":
                posted_at = ""

            jobs.append({
                "source": "JobSpy",
                "title": title,
                "company": str(row.get("company_name", row.get("company", ""))),
                "location": location,
                "url": url,
                "tags": tags,
                "description": str(row.get("description", ""))[:3000],
                "remote": is_remote,
                "salary_min": salary_min,
                "posted_at": posted_at,
            })

    except Exception as e:
        log.warning(f"  JobSpy failed: {e}")

    log.info(f"  JobSpy: {len(jobs)} listings fetched")
    return jobs
