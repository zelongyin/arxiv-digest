#!/usr/bin/env python3
"""
arXiv digest.

Fetches new arXiv papers in the categories listed in config.toml, asks Claude
to score each one against your research interests, writes short summaries of
the best ones, and emails you the result.

Usage:
    python digest.py                   # build the digest and email it
    python digest.py --dry-run         # build digest.html but don't send anything
    python digest.py --limit 40        # only score the first 40 papers (cheap test)
    python digest.py --scheduled --last-sent sent-20261014T1800Z
                                       # send only if a digest is due and not yet delivered
    python digest.py --check ...       # same inputs, just print send=true/false (used by the workflow)

Environment variables (set these as GitHub repository secrets):
    ANTHROPIC_API_KEY    Claude API key
    GMAIL_ADDRESS        Gmail address the digest is sent from
    GMAIL_APP_PASSWORD   16-character Gmail app password
    DIGEST_TO            optional; where to send it (comma-separated is fine).
                         Defaults to GMAIL_ADDRESS.

Requires Python 3.11+.
"""

from __future__ import annotations

import argparse
import datetime as dt
import html
import http.client
import json
import math
import os
import re
import smtplib
import ssl
import sys
import time
import tomllib
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from email.headerregistry import Address
from email.message import EmailMessage
from email.utils import formatdate, make_msgid, parsedate_to_datetime
from pathlib import Path

# `anthropic` is imported inside Claude() so that --check works
# before the dependencies are installed.

HERE = Path(__file__).resolve().parent

ARXIV_API = "https://export.arxiv.org/api/query"
USER_AGENT = "arxiv-digest/1.1 (personal research digest)"
ARXIV_DELAY_S = 3.5  # arXiv asks for no more than one request every 3 seconds
# The API allows up to 2,000 results per request, but its manual asks clients
# to keep to about 1,000. A 3-day window of five categories usually fits in
# one or two requests.
ARXIV_PAGE_SIZE = 1000
ATOM = "{http://www.w3.org/2005/Atom}"
OPENSEARCH = "{http://a9.com/-/spec/opensearch/1.1/}"

# USD per million tokens (input, output). Only used for the cost line in the
# log and email footer; unknown models just skip the estimate.
PRICES = {"claude-haiku-5-5": (0.10, 0.50)}

DEFAULTS = {
    "feed": {
        "name": "arXiv digest",
        "icon": "",
        "every_days": 7,
        "first_date": "2026-10-19",
        "articles_per_day": 15,
        "max_articles": 100,
        "summary_words": 40,
        "why_words": 20,
    },
    "arxiv": {"categories": ["quant-ph"]},
    "interests": {
        "description": "",
        "topics": [],
        "always_include_authors": [],
        "always_include_keywords": [],
        "exclude_keywords": [],
    },
    "selection": {"min_score": 6, "also_notable": 10, "also_notable_min_score": 4},
    "timing": {"extra_delay_days": 0, "arxiv_max_wait_minutes": 40, "retry_max_wait_minutes": 10},
    "claude": {
        "scoring_model": "claude-haiku-5-5",
        "summary_model": "claude-haiku-5-5",
        "batch_size": 20,
        "summary_batch_size": 5,
    },
    "email": {
        "subject": "{icon} {name} · {papers} · {dates}",
        "smtp_host": "smtp.gmail.com",
        "smtp_port": 465,
        "attach_html_copy": False,
        "save_digest_copy": True,
    },
}


def log(msg: str) -> None:
    print(msg, flush=True)


def clean(text: str | None) -> str:
    """Collapse whitespace and newlines into single spaces."""
    return " ".join((text or "").split())


def normalize_id(raw: str) -> str:
    """'arXiv:2610.01234v2 ' -> '2610.01234'"""
    raw = raw.strip()
    raw = re.sub(r"^arxiv:", "", raw, flags=re.IGNORECASE)
    return re.sub(r"v\d+$", "", raw)


def fmt_day(d: dt.date | dt.datetime) -> str:
    return f"{d:%b} {d.day}"


def fmt_range(first: dt.date, last: dt.date, year: bool = True) -> str:
    """'Oct 5–11, 2026', 'Sep 28 – Oct 4, 2026', or 'Oct 11, 2026' for one day."""
    if first == last:
        out = fmt_day(first)
    elif (first.year, first.month) == (last.year, last.month):
        out = f"{fmt_day(first)}–{last.day}"
    elif first.year != last.year and year:
        return f"{fmt_day(first)}, {first.year} – {fmt_day(last)}, {last.year}"
    else:
        out = f"{fmt_day(first)} – {fmt_day(last)}"
    return f"{out}, {last.year}" if year else out


def plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


def trim_words(text: str, limit: int) -> str:
    """Safety net in case the model runs well past the requested length."""
    words = text.split()
    if limit <= 0 or len(words) <= int(limit * 1.3) + 2:
        return text
    return " ".join(words[:limit]).rstrip(",;:–—-") + "…"


class _KeepMissing(dict):
    """For str.format_map: leave unknown {placeholders} as they are."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


# --------------------------------------------------------------------------
# Data
# --------------------------------------------------------------------------


@dataclass
class Paper:
    id: str
    title: str
    abstract: str
    authors: list[str]
    categories: list[str]
    published: dt.datetime
    score: int = 0
    topic: str = ""
    reason: str = ""
    summary: str = ""
    why: str = ""
    forced: bool = False

    @property
    def abs_url(self) -> str:
        return f"https://arxiv.org/abs/{self.id}"

    @property
    def pdf_url(self) -> str:
        return f"https://arxiv.org/pdf/{self.id}"

    def author_line(self, n: int = 3) -> str:
        if len(self.authors) <= n:
            return ", ".join(self.authors)
        return ", ".join(self.authors[:n]) + " et al."


# --------------------------------------------------------------------------
# Config, schedule and date window
# --------------------------------------------------------------------------


def _as_int(cfg: dict, section: str, key: str, minimum: int) -> int:
    value = cfg[section][key]
    try:
        number = int(value)
    except (TypeError, ValueError):
        sys.exit(f"config.toml: [{section}] {key} must be a whole number (got {value!r}).")
    if number < minimum:
        sys.exit(f"config.toml: [{section}] {key} must be at least {minimum} (got {number}).")
    return number


def load_config(path: Path) -> dict:
    with path.open("rb") as f:
        cfg = tomllib.load(f)
    for section, values in DEFAULTS.items():
        cfg.setdefault(section, {})
        for key, value in values.items():
            cfg[section].setdefault(key, value)

    feed = cfg["feed"]
    feed["every_days"] = _as_int(cfg, "feed", "every_days", 1)
    feed["max_articles"] = _as_int(cfg, "feed", "max_articles", 1)
    try:
        feed["articles_per_day"] = float(feed["articles_per_day"])
    except (TypeError, ValueError):
        sys.exit(f"config.toml: [feed] articles_per_day must be a number (got {feed['articles_per_day']!r}).")
    if feed["articles_per_day"] <= 0:
        sys.exit("config.toml: [feed] articles_per_day must be more than 0.")
    feed["summary_words"] = _as_int(cfg, "feed", "summary_words", 5)
    feed["why_words"] = _as_int(cfg, "feed", "why_words", 5)
    first = feed["first_date"]
    if isinstance(first, dt.datetime):
        first = first.date()
    elif not isinstance(first, dt.date):
        try:
            first = dt.date.fromisoformat(str(first).strip())
        except ValueError:
            sys.exit(f"config.toml: [feed] first_date must look like 2026-10-19 (got {first!r}).")
    feed["first_date"] = first
    feed["name"] = clean(str(feed["name"])) or "arXiv digest"
    feed["icon"] = clean(str(feed["icon"]))
    cfg["timing"]["extra_delay_days"] = _as_int(cfg, "timing", "extra_delay_days", 0)
    cfg["timing"]["arxiv_max_wait_minutes"] = _as_int(cfg, "timing", "arxiv_max_wait_minutes", 1)
    cfg["timing"]["retry_max_wait_minutes"] = _as_int(cfg, "timing", "retry_max_wait_minutes", 1)

    if not cfg["interests"]["description"].strip():
        sys.exit("config.toml: [interests] description is empty.")
    if not cfg["arxiv"]["categories"]:
        sys.exit("config.toml: [arxiv] categories is empty.")
    return cfg


# arXiv's schedule (US Eastern time): new submissions are accepted until 14:00
# on weekdays. Papers submitted before a Monday-Thursday deadline are announced
# at 20:00 that same day; papers submitted before the Friday deadline are
# announced on Sunday at 20:00. Nothing is announced on Friday or Saturday.
ANNOUNCE_MARGIN = dt.timedelta(minutes=30)  # announcements sometimes go out a little late


def latest_announced_cutoff(t: dt.datetime) -> dt.datetime:
    """The submission deadline covered by arXiv's newest announcement before t.

    Every paper submitted before the returned time (UTC) had been publicly
    announced by t, apart from papers held for moderation or delayed by an
    arXiv holiday.
    """
    from zoneinfo import ZoneInfo  # uses Linux's built-in time-zone data (tzdata package on Windows)

    eastern = ZoneInfo("America/New_York")
    t_et = t.astimezone(eastern)
    for days_back in range(8):
        day = t_et.date() - dt.timedelta(days=days_back)
        if day.weekday() >= 5:  # no deadline on Saturday or Sunday
            continue
        announce_day = day + dt.timedelta(days=2) if day.weekday() == 4 else day
        announced = dt.datetime.combine(announce_day, dt.time(20, 0), tzinfo=eastern)
        if announced + ANNOUNCE_MARGIN <= t_et:
            deadline = dt.datetime.combine(day, dt.time(14, 0), tzinfo=eastern)
            return deadline.astimezone(dt.timezone.utc)
    raise RuntimeError("unreachable: arXiv announces at least once a week")


def compute_window(
    days: int, extra_delay_days: int, now: dt.datetime | None = None
) -> tuple[dt.datetime, dt.datetime]:
    """Return [start, end) in UTC: everything arXiv announced since the last digest.

    The window ends at the submission deadline of arXiv's newest announcement
    and starts where the window of a digest `days` earlier would have ended,
    so digests on a regular schedule line up exactly: no gaps, no repeats.
    `extra_delay_days` shifts the whole window back for extra safety (e.g.
    around arXiv holidays). The result is empty if arXiv announced nothing in
    between (e.g. a daily digest on a weekend).
    """
    now = (now or dt.datetime.now(dt.timezone.utc)) - dt.timedelta(days=extra_delay_days)
    end = latest_announced_cutoff(now)
    start = latest_announced_cutoff(now - dt.timedelta(days=days))
    return start, end


# --------------------------------------------------------------------------
# Deciding what to send: schedule, receipts and retries
# --------------------------------------------------------------------------
#
# The workflow runs every 4 hours. After each digest is sent, it saves a
# small "receipt" (a GitHub artifact named sent-YYYYMMDDTHHMMZ) recording the
# submission time up to which papers have been delivered. Each run then asks:
# is a scheduled digest due whose papers haven't all been delivered yet? If
# so, it sends everything from the last receipt up to that digest's cutoff.
# So a failed digest is retried on the next run, and if it never gets
# through, its papers roll into the next digest instead of being lost.

SEND_HOUR_UTC = 14  # a digest is due from this hour on; keep in step with the first cron line
MAX_CATCH_UP = dt.timedelta(days=14)  # never reach back further than this for missed papers


def parse_receipt(value: str | None) -> dt.datetime | None:
    """'sent-20261014T1800Z' (or just '20261014T1800Z') -> datetime in UTC."""
    value = (value or "").strip()
    if value.startswith("sent-"):
        value = value[len("sent-"):]
    if not value:
        return None
    try:
        return dt.datetime.strptime(value, "%Y%m%dT%H%MZ").replace(tzinfo=dt.timezone.utc)
    except ValueError:
        print(f"  ignoring an unreadable receipt: {value!r}", file=sys.stderr)
        return None


def receipt_name(until: dt.datetime) -> str:
    return f"sent-{until.astimezone(dt.timezone.utc):%Y%m%dT%H%MZ}"


def latest_due_date(feed: dict, now: dt.datetime) -> dt.date | None:
    """The most recent scheduled digest date whose send time has passed."""
    day = now.date()
    if now.hour < SEND_HOUR_UTC:
        day -= dt.timedelta(days=1)  # today's digest isn't due yet
    delta = (day - feed["first_date"]).days
    if delta < 0:
        return None
    return day - dt.timedelta(days=delta % feed["every_days"])


@dataclass
class Plan:
    send: bool
    reason: str
    start: dt.datetime | None = None
    end: dt.datetime | None = None
    hours_late: float = 0.0
    catching_up: bool = False  # includes papers from an earlier digest that didn't get through
    write_receipt: bool = True
    skipped_before: dt.datetime | None = None  # set if the gap was too long to catch up fully


def catch_up_start(
    last_sent: dt.datetime, regular_start: dt.datetime, end: dt.datetime
) -> tuple[dt.datetime, bool, dt.datetime | None]:
    """Where a digest should start, given the newest receipt.

    Normally right at the receipt, so nothing is skipped or repeated. After a
    long gap, no further back than MAX_CATCH_UP (or one regular window, if
    that's longer). Returns (start, catching_up, skipped_before).
    """
    earliest = min(regular_start, end - MAX_CATCH_UP)
    start = max(last_sent, earliest)
    return start, start < regular_start, (earliest if last_sent < earliest else None)


def plan_scheduled(
    cfg: dict, now: dt.datetime, last_sent: dt.datetime | None, receipts_known: bool, is_retry: bool
) -> Plan:
    feed, delay = cfg["feed"], cfg["timing"]["extra_delay_days"]
    due_date = latest_due_date(feed, now)
    if due_date is None:
        return Plan(False, f"The first digest is due on {feed['first_date']:%a %Y-%m-%d}.")
    due_at = dt.datetime.combine(due_date, dt.time(SEND_HOUR_UTC, 0), tzinfo=dt.timezone.utc)
    hours_late = max(0.0, (now - due_at).total_seconds() / 3600)
    nominal = dt.datetime.combine(due_date, dt.time(12, 0), tzinfo=dt.timezone.utc)
    regular_start, end = compute_window(feed["every_days"], delay, now=nominal)

    skipped_before = None
    if not receipts_known:
        # Without receipts we can't tell whether we'd be repeating a digest,
        # so only the regular run on the due date itself sends.
        if is_retry or due_date != now.date():
            return Plan(False, "Couldn't look up earlier digests, so not sending from this run.")
        start, catching_up = regular_start, False
    elif last_sent is not None and last_sent >= end:
        if regular_start >= end:
            return Plan(False, f"arXiv hasn't announced anything new for the {due_date:%a %b %d} digest.")
        return Plan(False, f"The {due_date:%a %b %d} digest has already been sent.")
    elif last_sent is not None:
        start, catching_up, skipped_before = catch_up_start(last_sent, regular_start, end)
    elif due_date > feed["first_date"]:
        # No receipt yet although digests are due: the first one(s) never got
        # through. Reach back to the first digest's window (up to 14 days).
        first_noon = dt.datetime.combine(feed["first_date"], dt.time(12, 0), tzinfo=dt.timezone.utc)
        first_start, _ = compute_window(feed["every_days"], delay, now=first_noon)
        if end - first_start <= MAX_CATCH_UP:
            start, catching_up = first_start, first_start < regular_start
        else:
            start, catching_up = regular_start, False
    else:
        start, catching_up = regular_start, False

    if start >= end:
        return Plan(False, "arXiv hasn't announced anything new since the last digest.")
    verb = "Retrying" if is_retry else "Sending"
    return Plan(
        True, f"{verb} the {due_date:%a %b %d} digest.", start, end, hours_late, catching_up,
        skipped_before=skipped_before,
    )


def plan_manual(cfg: dict, now: dt.datetime, last_sent: dt.datetime | None, test: bool) -> Plan:
    """Manual runs always send. Full runs pick up from the last receipt, so
    they don't repeat papers; test runs (--limit or --dry-run) just take the
    last few days and leave the receipts alone."""
    feed, delay = cfg["feed"], cfg["timing"]["extra_delay_days"]
    days = max(feed["every_days"], 3) if test else feed["every_days"]
    start, end = compute_window(days, delay, now=now)
    catching_up, skipped_before = False, None
    if not test and last_sent is not None and last_sent < end:
        start, catching_up, skipped_before = catch_up_start(last_sent, start, end)
    if start >= end:
        start, _ = compute_window(max(feed["every_days"], 3), delay, now=now)
    return Plan(
        True, "Manual run.", start, end, 0.0, catching_up,
        write_receipt=not test, skipped_before=skipped_before,
    )


# --------------------------------------------------------------------------
# arXiv
# --------------------------------------------------------------------------

#
# arXiv sometimes rate-limits GitHub's shared runner IPs for 20-45 minutes at
# a time (HTTP 429 "Rate exceeded.", usually with no Retry-After header), even
# for a client that sends very few requests. So: keep retrying patiently
# within an overall time budget, resume from the batch that failed, and send
# a partial digest rather than none (the rest follows in the next one).

RETRY_CODES = (429, 500, 502, 503, 504)

_last_arxiv_request = 0.0
_arxiv_deadline = float("inf")


class ArxivUnavailable(RuntimeError):
    """arXiv kept refusing requests (rate limiting or outage)."""


def set_arxiv_wait_budget(minutes: float) -> None:
    global _arxiv_deadline
    _arxiv_deadline = time.monotonic() + minutes * 60


def _retry_after_seconds(headers) -> float | None:
    value = headers.get("Retry-After") if headers is not None else None
    if not value:
        return None
    try:
        seconds = float(value)
    except ValueError:
        try:
            seconds = (parsedate_to_datetime(value) - dt.datetime.now(dt.timezone.utc)).total_seconds()
        except (TypeError, ValueError):
            return None
    return max(ARXIV_DELAY_S, min(seconds, 600.0))


def arxiv_get(params: dict) -> ET.Element:
    """GET the arXiv API politely, with retries. Returns the parsed Atom feed.

    If arXiv refuses, keeps retrying, with waits growing from 30 s to 5 min,
    until the overall budget set by set_arxiv_wait_budget() is used up, then
    makes one last attempt at the deadline. Raises ArxivUnavailable if that
    fails too.
    """
    global _last_arxiv_request
    url = ARXIV_API + "?" + urllib.parse.urlencode(params, safe=":")
    backoff = 30.0
    err = "unknown error"
    for attempt in range(1, 500):  # in practice the time budget ends it long before this
        wait = ARXIV_DELAY_S - (time.monotonic() - _last_arxiv_request)
        if wait > 0:
            time.sleep(wait)
        _last_arxiv_request = time.monotonic()
        retry_after = None
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=180) as resp:  # big responses can be slow
                body = resp.read()
            # Rate limiting can come back as plain text, so make sure it parses.
            return ET.fromstring(body)
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_CODES:
                raise
            retry_after = _retry_after_seconds(e.headers)
            try:
                text = clean(e.read(200).decode("utf-8", "replace"))
            except Exception:
                text = ""
            err = f"HTTP {e.code}" + (f' "{text[:60]}"' if text else "")
        except (OSError, http.client.HTTPException, ET.ParseError) as e:
            err = f"{type(e).__name__}: {e}"
        remaining = _arxiv_deadline - time.monotonic()
        if remaining <= 0:
            raise ArxivUnavailable(f"{err}; still refused after the full wait ({attempt} attempts)")
        # Wait at least as long as arXiv asks, but never less than the current
        # backoff: hammering a rate limit only prolongs it. The last wait is
        # cut short so the final attempt lands right at the deadline.
        pause = min(max(retry_after or 0.0, backoff), remaining)
        log(
            f"  arXiv request failed ({err}); waiting {pause:.0f}s "
            f"(attempt {attempt}, {remaining / 60:.0f} min of waiting left)"
        )
        time.sleep(pause)
        backoff = min(backoff * 2, 300.0)
    raise ArxivUnavailable(f"{err}; gave up after {attempt} attempts")


def parse_feed(root: ET.Element) -> tuple[list[Paper], int]:
    total = int(root.findtext(f"{OPENSEARCH}totalResults") or 0)
    papers = []
    for entry in root.findall(f"{ATOM}entry"):
        raw_id = (entry.findtext(f"{ATOM}id") or "").strip()
        if "/api/errors" in raw_id:
            raise RuntimeError("arXiv API error: " + clean(entry.findtext(f"{ATOM}summary")))
        if "/abs/" not in raw_id:
            continue
        published_raw = (entry.findtext(f"{ATOM}published") or "").strip()
        if not published_raw:
            continue
        papers.append(
            Paper(
                id=normalize_id(raw_id.split("/abs/", 1)[1]),
                title=clean(entry.findtext(f"{ATOM}title")),
                abstract=clean(entry.findtext(f"{ATOM}summary")),
                authors=[clean(a.findtext(f"{ATOM}name")) for a in entry.findall(f"{ATOM}author")],
                categories=[c.get("term") for c in entry.findall(f"{ATOM}category") if c.get("term")],
                published=dt.datetime.fromisoformat(published_raw.replace("Z", "+00:00")),
            )
        )
    return papers, total


def _fetch_range(
    cat_query: str, a: dt.datetime, b: dt.datetime, found: dict[str, Paper], progress: dict
) -> None:
    """Add the papers first submitted in [a, b) to `found`.

    Asks for up to ARXIV_PAGE_SIZE papers per request, oldest first. If the
    range holds more, the next request starts from the submission time of the
    last paper received. That avoids paging with `start`, which the API
    handles unreliably, and a range of N papers takes about N / 1,000 requests.

    `progress["cursor"]` remembers how far it got, so if a request fails and
    this is called again, it resumes there instead of starting over.
    """
    cursor = progress.setdefault("cursor", a)
    while True:
        # arXiv's end minute is inclusive, so ask up to b and drop anything at
        # or after b below. The next digest's window starts at b.
        query = f"({cat_query}) AND submittedDate:[{cursor:%Y%m%d%H%M} TO {b:%Y%m%d%H%M}]"
        params = {
            "search_query": query,
            "start": 0,
            "max_results": ARXIV_PAGE_SIZE,
            "sortBy": "submittedDate",
            "sortOrder": "ascending",
        }
        batch, total = parse_feed(arxiv_get(params))
        for attempt in range(1, 3):
            # The API occasionally returns a short or empty result by mistake.
            short = len(batch) < min(total, ARXIV_PAGE_SIZE)
            suspicious_empty = total == 0 and b - cursor >= dt.timedelta(hours=12)
            if not (short or suspicious_empty):
                break
            log(f"  arXiv returned {len(batch)} of {total} papers; retrying")
            time.sleep(10 * attempt)
            batch, total = parse_feed(arxiv_get(params))
        else:
            # Still empty or short after the retries: treat it like a refusal,
            # so the papers are fetched again later instead of being skipped.
            short = len(batch) < min(total, ARXIV_PAGE_SIZE)
            if short or (total == 0 and b - cursor >= dt.timedelta(hours=12)):
                raise ArxivUnavailable(f"arXiv kept returning {len(batch)} of {total} papers")

        for p in batch:
            if a <= p.published < b:
                found.setdefault(p.id, p)
        if not batch or total <= len(batch):
            progress["done"] = True
            return

        # More to come: continue from the last paper's minute. Papers in that
        # minute come back again and are skipped as duplicates.
        nxt = max(p.published for p in batch).replace(second=0, microsecond=0)
        if nxt <= cursor:
            log("  warning: over 1,000 papers in a single minute; some may be missing")
            progress["done"] = True
            return
        cursor = progress["cursor"] = nxt
        log(f"  received {len(batch)} of {total}; continuing from {cursor:%a %b %d %H:%M} UTC")


def fetch_papers(
    categories: list[str], start: dt.datetime, end: dt.datetime
) -> tuple[list[Paper], dt.datetime]:
    """All papers first submitted in [start, end), in as few requests as possible.

    Returns the papers and the time up to which the list is complete: `end`
    normally, or earlier if arXiv stopped serving requests partway through.
    Raises ArxivUnavailable if arXiv served nothing at all.
    """
    cat_query = " OR ".join(f"cat:{c}" for c in categories)
    found: dict[str, Paper] = {}
    progress: dict = {}  # shared between attempts, so a retry resumes where the last one stopped
    for attempt in (1, 2):
        try:
            _fetch_range(cat_query, start, end, found, progress)
            log(f"  {len(found)} papers")
            return sorted(found.values(), key=lambda p: p.published), end
        except ArxivUnavailable as e:
            if attempt == 1:
                log(f"  arXiv unavailable ({e}); have {len(found)} papers so far, trying once more")
            else:
                log(f"  arXiv still unavailable ({e}); giving up with {len(found)} papers")
    cursor = progress.get("cursor", start)
    # Requests go oldest first, so the list is complete up to the start of the
    # batch that didn't come through. Leave out papers from that minute on;
    # they'll all come next time, so none are sent twice.
    partial = [p for p in found.values() if p.published < cursor]
    if not partial:
        raise ArxivUnavailable("arXiv refused every request (rate limiting), so there's nothing to send")
    return sorted(partial, key=lambda p: p.published), cursor


# --------------------------------------------------------------------------
# Rules from config (always include / exclude)
# --------------------------------------------------------------------------


def _norm(text: str) -> str:
    """Lowercase and treat hyphens as spaces, so 'two-block' matches 'two block'."""
    return " ".join(text.lower().replace("-", " ").split())


def apply_rules(papers: list[Paper], interests: dict) -> tuple[list[Paper], int]:
    include_kw = [_norm(k) for k in interests["always_include_keywords"] if k.strip()]
    exclude_kw = [_norm(k) for k in interests["exclude_keywords"] if k.strip()]
    # Whole-word match on author names, so "Li" doesn't match "Lin" or "Olivier".
    include_auth = [
        re.compile(rf"\b{re.escape(_norm(a))}\b") for a in interests["always_include_authors"] if a.strip()
    ]
    kept, dropped = [], 0
    for p in papers:
        text = _norm(f"{p.title} {p.abstract}")
        names = [_norm(a) for a in p.authors]
        if any(k in text for k in include_kw) or any(r.search(n) for r in include_auth for n in names):
            p.forced = True
        elif any(k in text for k in exclude_kw):
            dropped += 1
            continue
        kept.append(p)
    return kept, dropped


# --------------------------------------------------------------------------
# Claude
# --------------------------------------------------------------------------

SCORE_TOOL = {
    "name": "record_scores",
    "description": "Record a relevance score for every paper in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "scores": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The paper id exactly as given."},
                        "score": {"type": "integer", "minimum": 0, "maximum": 10},
                        "topic": {"type": "string"},
                        "reason": {"type": "string", "description": "A few words explaining the score."},
                    },
                    "required": ["id", "score", "topic"],
                },
            }
        },
        "required": ["scores"],
    },
}

SUMMARY_TOOL = {
    "name": "record_summaries",
    "description": "Record a summary for every paper in the batch.",
    "input_schema": {
        "type": "object",
        "properties": {
            "summaries": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string", "description": "The paper id exactly as given."},
                        "summary": {"type": "string"},
                        "why_relevant": {"type": "string"},
                    },
                    "required": ["id", "summary", "why_relevant"],
                },
            }
        },
        "required": ["summaries"],
    },
}


class Claude:
    def __init__(self) -> None:
        import anthropic

        # Reads ANTHROPIC_API_KEY from the environment. The SDK retries
        # rate-limit and server errors on its own.
        self.client = anthropic.Anthropic(max_retries=5, timeout=180)
        # Errors that retrying won't fix (bad key, no credit, unknown model, ...).
        self.fatal_errors = (
            anthropic.AuthenticationError,
            anthropic.PermissionDeniedError,
            anthropic.NotFoundError,
            anthropic.BadRequestError,
        )
        self.usage: dict[str, list[int]] = {}

    def call_tool(self, model: str, system: str, user: str, tool: dict, max_tokens: int) -> dict:
        resp = self.client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system,
            tools=[tool],
            tool_choice={"type": "tool", "name": tool["name"]},
            messages=[{"role": "user", "content": user}],
        )
        used = self.usage.setdefault(model, [0, 0])
        used[0] += resp.usage.input_tokens
        used[1] += resp.usage.output_tokens
        if resp.stop_reason == "max_tokens":
            log("  warning: response hit max_tokens; some papers may be missing")
        for block in resp.content:
            if block.type == "tool_use":
                return block.input
        raise RuntimeError(f"no tool output (stop_reason={resp.stop_reason})")

    def cost_line(self) -> str:
        tokens_in = sum(u[0] for u in self.usage.values())
        tokens_out = sum(u[1] for u in self.usage.values())
        line = f"{tokens_in:,} input + {tokens_out:,} output tokens"
        if self.usage and all(m in PRICES for m in self.usage):
            usd = sum(i / 1e6 * PRICES[m][0] + o / 1e6 * PRICES[m][1] for m, (i, o) in self.usage.items())
            line += f" (about ${usd:.2f})"
        return line


def paper_block(p: Paper) -> str:
    return (
        f'<paper id="{p.id}">\n'
        f"Title: {p.title}\n"
        f"Authors: {p.author_line(8)}\n"
        f"Categories: {', '.join(p.categories)}\n"
        f"Abstract: {p.abstract}\n"
        f"</paper>"
    )


def tool_list(out: dict, key: str) -> list[dict]:
    """Pull a list of dicts out of a tool result, tolerating a JSON-string list."""
    value = out.get(key, []) if isinstance(out, dict) else []
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            value = []
    return [item for item in value if isinstance(item, dict)] if isinstance(value, list) else []


def chunks(items: list, size: int) -> list[list]:
    size = max(1, int(size))
    return [items[i : i + size] for i in range(0, len(items), size)]


def scoring_prompt(interests: dict) -> str:
    topics = [t for t in interests["topics"] if t.strip()]
    topic_line = "; ".join(topics + ["Other"]) if topics else "a 2-4 word label of your choice"
    return f"""You screen new arXiv papers for a researcher. Their research interests, in their words:

<interests>
{interests['description'].strip()}
</interests>

For every paper, give:
- score: an integer 0-10 for how much this researcher would want to read it.
  9-10 = core to their work, a must-read. 7-8 = clearly relevant.
  5-6 = related, worth a look. 2-4 = same broad field but not their focus.
  0-1 = unrelated.
  Be strict: in a typical batch most papers should score 4 or lower.
- topic: the best-fitting label from this list: {topic_line}
- reason: a few words explaining the score.

Judge only from the title, authors and abstract. Score every paper you are
given, and use each paper's id exactly as written."""


def summary_prompt(interests: dict, summary_words: int, why_words: int) -> str:
    return f"""You write a short arXiv digest that the reader skims on their phone. The reader's research interests, in their words:

<interests>
{interests['description'].strip()}
</interests>

For each paper, write:
- summary: at most {summary_words} words. What the authors did and their main
  result. Include the single most important number (a fidelity, coherence
  time, threshold, code parameter, ...) if the abstract gives one. Plain and
  direct: no hype, and don't start with "This paper" or "The authors".
- why_relevant: at most {why_words} words, written to the reader as "you".
  Say why this paper matters for their work. Don't repeat the summary.

Stay within the word limits. Use only what the title and abstract say, and
use each paper's id exactly as written."""


def score_papers(claude: Claude, papers: list[Paper], cfg: dict) -> None:
    model = cfg["claude"]["scoring_model"]
    system = scoring_prompt(cfg["interests"])
    batches = chunks(papers, cfg["claude"]["batch_size"])
    failures, done = 0, 0
    for i, batch in enumerate(batches, 1):
        done += len(batch)
        user = "Score these papers:\n\n" + "\n\n".join(paper_block(p) for p in batch)
        try:
            out = claude.call_tool(model, system, user, SCORE_TOOL, max_tokens=150 * len(batch) + 1000)
        except claude.fatal_errors:
            raise
        except Exception as e:  # one bad batch shouldn't sink the whole digest
            failures += 1
            log(f"  scoring batch {i}/{len(batches)} failed: {e}")
            continue
        by_id = {p.id: p for p in batch}
        seen = set()
        for item in tool_list(out, "scores"):
            pid = normalize_id(str(item.get("id", "")))
            p = by_id.get(pid)
            if p is None:
                continue
            seen.add(pid)
            try:
                p.score = max(0, min(10, int(float(item.get("score", 0)))))
            except (TypeError, ValueError):
                p.score = 0
            p.topic = clean(str(item.get("topic", "")))
            p.reason = clean(str(item.get("reason", "")))
        if len(seen) < len(batch):
            log(f"  batch {i}: {len(batch) - len(seen)} papers got no score (treated as 0)")
        if i % 5 == 0 or i == len(batches):
            log(f"  processed {done}/{len(papers)}")
    if batches and failures == len(batches):
        raise RuntimeError("Every scoring request failed; see the errors above.")


def summarize_papers(claude: Claude, papers: list[Paper], cfg: dict) -> None:
    model = cfg["claude"]["summary_model"]
    s_words, w_words = cfg["feed"]["summary_words"], cfg["feed"]["why_words"]
    system = summary_prompt(cfg["interests"], s_words, w_words)
    per_paper = 3 * (s_words + w_words) + 150  # generous token budget per paper
    for batch in chunks(papers, cfg["claude"]["summary_batch_size"]):
        user = "Summarize these papers:\n\n" + "\n\n".join(paper_block(p) for p in batch)
        try:
            out = claude.call_tool(model, system, user, SUMMARY_TOOL, max_tokens=per_paper * len(batch) + 1000)
        except claude.fatal_errors:
            raise
        except Exception as e:
            log(f"  summary batch failed ({e}); using abstract excerpts instead")
            out = {}
        by_id = {p.id: p for p in batch}
        for item in tool_list(out, "summaries"):
            p = by_id.get(normalize_id(str(item.get("id", ""))))
            if p is not None:
                p.summary = trim_words(clean(str(item.get("summary", ""))), s_words)
                p.why = trim_words(clean(str(item.get("why_relevant", ""))), w_words)
        for p in batch:
            if not p.summary:
                # Fallback: the start of the abstract, cut to the word limit.
                words = p.abstract.split()
                p.summary = " ".join(words[:s_words]) + ("…" if len(words) > s_words else "")


def article_limit(feed: dict, start: dt.datetime, until: dt.datetime) -> int:
    """articles_per_day times the days of submissions covered, capped at max_articles."""
    days = max(0.0, (until - start).total_seconds() / 86400)
    return max(1, min(feed["max_articles"], math.ceil(feed["articles_per_day"] * days - 1e-9)))


def select(papers: list[Paper], cfg: dict, limit: int) -> tuple[list[Paper], list[Paper]]:
    """The main list (at most `limit` papers) and, separately, the
    "Also worth a glance" titles, which don't count toward the limit."""
    sel = cfg["selection"]
    ranked = sorted(papers, key=lambda p: (p.forced, p.score), reverse=True)
    picks = [p for p in ranked if p.forced or p.score >= sel["min_score"]][:limit]
    picked = {p.id for p in picks}
    notable = [p for p in ranked if p.id not in picked and p.score >= sel["also_notable_min_score"]]
    picks.sort(key=lambda p: p.score, reverse=True)
    return picks, notable[: max(0, int(sel["also_notable"]))]


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

INK = "#1c1917"
MUTED = "#78716c"
SOFT = "#57534e"
LINK = "#1d4ed8"
ACCENT = "#7c3aed"


def tags_for(p: Paper, long: bool = False) -> str:
    parts = [p.topic] if p.topic else []
    parts.append(f"relevance {p.score}/10" if long else f"{p.score}/10")
    if p.forced:
        parts.append("★ watch list" if long else "★")
    return " · ".join(parts)


def build_html(picks: list[Paper], notable: list[Paper], meta: dict) -> str:
    e = html.escape
    font = "-apple-system,'Segoe UI',Helvetica,Arial,sans-serif"
    out = [
        f"""<!doctype html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>{e(meta['name'])} · {e(meta['dates'])}</title></head>
<body style="margin:0;padding:0;background:#f5f5f4;">
<div style="max-width:640px;margin:0 auto;padding:22px 16px 28px 16px;background:#ffffff;color:{INK};font-family:{font};">
<a name="top" id="top"></a>
<div style="font-size:24px;line-height:1.2;font-weight:800;color:{ACCENT};margin:0 0 4px 0;">{e(meta['heading'])}</div>
<div style="font-size:13px;line-height:1.4;color:{SOFT};margin:0 0 18px 0;">{e(meta['subtitle'])}</div>
"""
    ]
    if meta.get("notice"):
        out.append(
            f'<div style="background:#fef3c7;color:#78350f;border-radius:8px;padding:10px 12px;'
            f'margin:0 0 18px 0;font-size:13px;line-height:1.45;">{e(meta["notice"])}</div>\n'
        )

    if not picks:
        out.append(f'<p style="font-size:14px;line-height:1.5;">{e(meta["empty_note"])}</p>\n')
    else:
        # 1. At-a-glance list of titles
        out.append(
            f'<div style="background:#f5f3ff;border-radius:10px;padding:14px 16px 8px 16px;margin:0 0 26px 0;">\n'
            f'<div style="font-size:11px;font-weight:700;letter-spacing:0.08em;text-transform:uppercase;'
            f'color:{ACCENT};margin:0 0 8px 0;">In this issue</div>\n'
            f'<ol style="margin:0;padding:0 0 0 20px;font-size:14px;line-height:1.4;">\n'
        )
        for i, p in enumerate(picks, 1):
            out.append(
                f'<li style="margin:0 0 8px 0;"><a href="#p{i}" style="color:{INK};text-decoration:none;">'
                f"{e(p.title)}</a>"
                f'<span style="display:block;font-size:12px;color:{MUTED};margin:1px 0 0 0;">'
                f"{e(tags_for(p))}</span></li>\n"
            )
        out.append("</ol>\n</div>\n")

        # 2. One short write-up per paper
        for i, p in enumerate(picks, 1):
            why = (
                f'<p style="font-size:14px;line-height:1.45;margin:0 0 8px 0;color:#44403c;">'
                f"<b>Why it matters:</b> {e(p.why)}</p>\n"
                if p.why
                else ""
            )
            out.append(
                f"""<div style="margin:0 0 18px 0;padding:0 0 16px 0;border-bottom:1px solid #e7e5e4;">
<a name="p{i}" id="p{i}"></a>
<div style="font-size:12px;color:{MUTED};margin:0 0 3px 0;">{i} · {e(tags_for(p, long=True))}</div>
<a href="{e(p.abs_url)}" style="font-size:16px;line-height:1.3;font-weight:600;color:{LINK};text-decoration:none;">{e(p.title)}</a>
<div style="font-size:12px;color:{MUTED};margin:3px 0 8px 0;">{e(p.author_line())}</div>
<p style="font-size:14px;line-height:1.45;margin:0 0 6px 0;">{e(p.summary)}</p>
{why}<div style="font-size:13px;"><a href="{e(p.abs_url)}" style="color:{LINK};">Abstract</a> &nbsp;·&nbsp; <a href="{e(p.pdf_url)}" style="color:{LINK};">PDF</a> &nbsp;·&nbsp; <a href="#top" style="color:{MUTED};text-decoration:none;">↑ Top</a></div>
</div>
"""
            )

    # 3. Near misses, titles only
    if notable:
        out.append(f'<div style="font-size:15px;font-weight:700;margin:6px 0 8px 0;">Also worth a glance</div>\n')
        out.append('<ul style="padding:0 0 0 18px;margin:0 0 20px 0;font-size:14px;line-height:1.45;">\n')
        for p in notable:
            out.append(
                f'<li style="margin:0 0 5px 0;"><a href="{e(p.abs_url)}" style="color:{LINK};">{e(p.title)}</a>'
                f'<span style="font-size:12px;color:{MUTED};"> &nbsp;{e(tags_for(p))}</span></li>\n'
            )
        out.append("</ul>\n")

    out.append(
        f'<p style="font-size:12px;line-height:1.5;color:{MUTED};margin:18px 0 0 0;">{e(meta["footer"])}</p>\n'
        "</div></body></html>\n"
    )
    return "".join(out)


def build_text(picks: list[Paper], notable: list[Paper], meta: dict) -> str:
    lines = [meta["heading"], meta["subtitle"], ""]
    if meta.get("notice"):
        lines += [f"NOTE: {meta['notice']}", ""]
    if not picks:
        lines += [meta["empty_note"], ""]
    else:
        lines.append("IN THIS ISSUE")
        for i, p in enumerate(picks, 1):
            lines.append(f"{i}. {p.title}  ({tags_for(p)})")
        lines.append("")
        for i, p in enumerate(picks, 1):
            lines.append(f"{i}. {p.title}")
            lines.append(f"   {tags_for(p, long=True)} · {p.author_line()}")
            lines.append(f"   {p.summary}")
            if p.why:
                lines.append(f"   Why it matters: {p.why}")
            lines.append(f"   {p.abs_url}")
            lines.append("")
    if notable:
        lines.append("ALSO WORTH A GLANCE")
        for p in notable:
            lines.append(f"- {p.title} ({tags_for(p)}) {p.abs_url}")
        lines.append("")
    lines.append(meta["footer"])
    return "\n".join(lines)


def send_email(subject: str, sender_name: str, html_body: str, text_body: str, email_cfg: dict) -> str:
    sender = os.environ["GMAIL_ADDRESS"].strip()
    password = os.environ["GMAIL_APP_PASSWORD"].replace(" ", "").strip()
    to = (os.environ.get("DIGEST_TO") or "").strip() or sender

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = Address(display_name=sender_name, addr_spec=sender)
    msg["To"] = to
    msg["Date"] = formatdate(localtime=False)
    msg["Message-ID"] = make_msgid(domain=sender.rsplit("@", 1)[-1])
    msg.set_content(text_body)
    msg.add_alternative(html_body, subtype="html")
    if email_cfg["attach_html_copy"]:
        msg.add_attachment(
            html_body.encode("utf-8"), maintype="text", subtype="html", filename="arxiv-digest.html"
        )

    host, port = email_cfg["smtp_host"], int(email_cfg["smtp_port"])
    context = ssl.create_default_context()
    sent = False
    try:
        if port == 465:
            with smtplib.SMTP_SSL(host, port, context=context, timeout=60) as server:
                server.login(sender, password)
                server.send_message(msg)
                sent = True
        else:
            with smtplib.SMTP(host, port, timeout=60) as server:
                server.starttls(context=context)
                server.login(sender, password)
                server.send_message(msg)
                sent = True
    except (smtplib.SMTPException, OSError) as e:
        if not sent:
            raise
        # The email went out; only saying goodbye to the server failed. Don't
        # fail the run, or the digest would be sent again.
        log(f"  (the email was sent; ignoring an error while disconnecting: {e})")
    return to


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Build and email an arXiv digest.")
    ap.add_argument("--config", default=str(HERE / "config.toml"), help="path to config.toml")
    ap.add_argument("--dry-run", action="store_true", help="build digest.html but don't send email")
    ap.add_argument("--limit", type=int, default=0, help="only score the first N papers (for testing)")
    ap.add_argument("--output", default=str(HERE / "digest.html"), help="where to save the HTML copy")
    ap.add_argument(
        "--scheduled",
        action="store_true",
        help="a scheduled run: send only if a digest is due and hasn't been fully delivered",
    )
    ap.add_argument(
        "--check",
        action="store_true",
        help="with the same inputs as --scheduled, just print send=true or send=false and exit",
    )
    ap.add_argument(
        "--last-sent",
        default="",
        help="the newest receipt, e.g. sent-20261014T1800Z (blank if there are none)",
    )
    ap.add_argument(
        "--receipts-unknown",
        action="store_true",
        help="the receipts couldn't be looked up; only the regular run on a due date sends",
    )
    ap.add_argument(
        "--retry",
        action="store_true",
        help="this is one of the retry runs (shorter arXiv wait; doesn't fail if arXiv is blocking)",
    )
    args = ap.parse_args()

    cfg = load_config(Path(args.config))
    feed = cfg["feed"]
    now = dt.datetime.now(dt.timezone.utc)
    last_sent = parse_receipt(args.last_sent)
    receipts_known = not args.receipts_unknown
    test = args.dry_run or args.limit > 0
    is_retry = args.retry

    if args.check:
        plan = plan_scheduled(cfg, now, last_sent, receipts_known, is_retry)
        print(f"send={'true' if plan.send else 'false'}")
        print(plan.reason, file=sys.stderr)
        return

    if not os.environ.get("ANTHROPIC_API_KEY"):
        sys.exit("ANTHROPIC_API_KEY is not set (add it as a repository secret).")
    if not args.dry_run and not (os.environ.get("GMAIL_ADDRESS") and os.environ.get("GMAIL_APP_PASSWORD")):
        sys.exit("GMAIL_ADDRESS and GMAIL_APP_PASSWORD must be set to send email (or use --dry-run).")

    if args.scheduled:
        plan = plan_scheduled(cfg, now, last_sent, receipts_known, is_retry)
    else:
        plan = plan_manual(cfg, now, last_sent, test)
        is_retry = False
    log(plan.reason)
    if not plan.send:
        return
    start, end = plan.start, plan.end
    cats = cfg["arxiv"]["categories"]

    from zoneinfo import ZoneInfo

    eastern = ZoneInfo("America/New_York")
    start_et, end_et = start.astimezone(eastern), end.astimezone(eastern)
    last_day = (end_et - dt.timedelta(minutes=1)).date()
    dates_long = fmt_range(start_et.date(), last_day, year=True)
    dates_short = fmt_range(start_et.date(), last_day, year=False)
    span = (
        f"{fmt_day(start_et)} {start_et:%H:%M} – {fmt_day(end_et)} {end_et:%H:%M} US Eastern"
    )
    log(f"Fetching {', '.join(cats)} papers submitted {span} ({start:%Y-%m-%d %H:%M} – {end:%Y-%m-%d %H:%M} UTC)")

    timing = cfg["timing"]
    set_arxiv_wait_budget(timing["retry_max_wait_minutes"] if is_retry else timing["arxiv_max_wait_minutes"])
    try:
        papers, complete_until = fetch_papers(cats, start, end)
    except ArxivUnavailable as e:
        if is_retry:
            # Don't mark retries as failed: that would send a GitHub failure
            # email every 4 hours while arXiv is blocking.
            log(f"{e}. Will try again at the next check, 4 hours from now.")
            return
        if args.scheduled:
            raise RuntimeError(f"{e}. The workflow will try again automatically in 4 hours.") from e
        raise RuntimeError(f"{e}. Try again later.") from e
    n_fetched = len(papers)
    log(f"Fetched {n_fetched} new papers")
    papers, n_dropped = apply_rules(papers, cfg["interests"])
    if n_dropped:
        log(f"Dropped {n_dropped} papers matching exclude_keywords")
    if args.limit > 0:
        papers = papers[: args.limit]
        log(f"--limit: scoring only the first {len(papers)} papers")

    claude = Claude()
    if papers:
        log(f"Scoring {len(papers)} papers with {cfg['claude']['scoring_model']}")
        score_papers(claude, papers, cfg)
    limit = article_limit(feed, start, complete_until)
    covered_days = (complete_until - start).total_seconds() / 86400
    log(f"Main list: up to {limit} papers ({feed['articles_per_day']:g} a day x {covered_days:.1f} days)")
    picks, notable = select(papers, cfg, limit)
    if picks:
        log(f"Summarizing {len(picks)} papers with {cfg['claude']['summary_model']}")
        summarize_papers(claude, picks, cfg)
    cost = claude.cost_line()
    log(f"Claude usage: {cost}")

    if n_fetched == 0:
        empty_note = (
            "arXiv returned no papers for this window. That usually means the arXiv API "
            "was having trouble; check the run log on GitHub."
        )
    else:
        empty_note = "Nothing cleared the relevance bar this time."
    notes = []
    if is_retry and plan.hours_late >= 1:
        notes.append(
            f"This digest is about {round(plan.hours_late)} hours late: an earlier attempt "
            f"didn't get through, most likely because arXiv was blocking requests."
        )
    if plan.skipped_before is not None:
        sb = plan.skipped_before.astimezone(eastern)
        notes.append(
            f"It's been over two weeks since the last digest got through, so this one catches up "
            f"on papers submitted since {fmt_day(sb)} only; older ones were skipped."
        )
    elif plan.catching_up:
        notes.append("It also includes papers from an earlier digest that couldn't be sent.")
    if complete_until < end:
        cu = complete_until.astimezone(eastern)
        notes.append(
            f"arXiv started blocking requests partway through, so papers submitted after "
            f"{fmt_day(cu)} {cu:%H:%M} US Eastern aren't in this one. They'll come in the next digest."
        )
        log(f"Warning: complete only up to {complete_until:%Y-%m-%d %H:%M} UTC")
    notice = " ".join(notes)
    heading = f"{feed['icon']} {feed['name']}".strip()
    meta = {
        "name": feed["name"],
        "heading": heading,
        "notice": notice,
        "dates": dates_long,
        "subtitle": (
            f"{dates_long} · {plural(len(picks), 'pick')} from "
            f"{plural(len(papers), 'new paper')} in {', '.join(cats)}"
        ),
        "empty_note": empty_note,
        "footer": (
            f"Covers every paper arXiv announced since the last digest (submitted {span}). "
            f"Ranked and summarized by Claude "
            f"({cfg['claude']['scoring_model']}); {cost}. To change what shows up, edit config.toml "
            f"in your repository."
        ),
    }
    html_body = build_html(picks, notable, meta)
    text_body = build_text(picks, notable, meta)
    if args.dry_run or cfg["email"]["save_digest_copy"]:
        # The workflow keeps this file as an artifact; turn it off in a public repo.
        Path(args.output).write_text(html_body, encoding="utf-8")
        log(f"Saved {args.output}")

    if args.dry_run:
        log("Dry run: email not sent.")
        return
    subject = cfg["email"]["subject"].format_map(
        _KeepMissing(
            icon=feed["icon"],
            name=feed["name"],
            count=len(picks),
            papers=plural(len(picks), "paper"),
            dates=dates_short,
        )
    )
    subject = clean(subject)
    to = send_email(subject, feed["name"], html_body, text_body, cfg["email"])
    log(f"Emailed the digest to {to}")

    if plan.write_receipt:
        # Record how far this digest got; the workflow saves it as an artifact.
        name = receipt_name(complete_until)
        (HERE / "receipt.txt").write_text(f"{complete_until.isoformat()}\n", encoding="utf-8")
        github_output = os.environ.get("GITHUB_OUTPUT")
        if github_output:
            with open(github_output, "a", encoding="utf-8") as f:
                f.write(f"receipt={name}\n")
        log(f"Receipt: {name}")


if __name__ == "__main__":
    main()
