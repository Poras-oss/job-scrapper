"""
sources — Job scraper source modules.

Each module exposes a `scrape(client, cfg)` function that returns
a list of standardized job dicts.
"""

from .remoteok import scrape as scrape_remoteok
from .weworkremotely import scrape as scrape_weworkremotely
from .arbeitnow import scrape as scrape_arbeitnow
from .adzuna import scrape as scrape_adzuna
from .reddit import scrape as scrape_reddit
from .google_jobs import scrape as scrape_google_jobs
from .hackernews import scrape as scrape_hackernews
from .jobspy_source import scrape as scrape_jobspy
