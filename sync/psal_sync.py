#!/usr/bin/env python3
"""
BXSCI Athletics — PSAL schedule & results sync
==============================================

Collects Bronx High School of Science (PSAL school code 10501) games from the
public pages of https://www.psal.org and writes them to `games.json` in the
format the website already uses.

Where the data comes from (all public pages, no login):
  1. School Sports Calendar  /events-and-standings/school-calendar.aspx?schid=10501
       -> every Bronx Science game: date, sport, boys/girls, varsity/JV,
          opponent, venue, PSAL game id. (No times or scores on this page.)
  2. Sport pages             /sports/sport.aspx?spCode=XXX&flag=All
       -> game time, which team is home, and status words such as
          "postponed" / "forfeit".
  3. Game detail pages       /games/game-detail.aspx#XXX/GAMEID
       -> status, time and GAME/MEET RESULTS. These are filled in by
          JavaScript, so they are rendered with a headless browser
          (Playwright). Only recent games without a score are checked.

Safety rules built in:
  * PSAL does not publish an API or data feed, and its site shows no terms
    of use. This script obeys https://www.psal.org/robots.txt on every run
    and stops if a page is disallowed.
  * It never retries around 401/403/429 responses, waits between requests,
    identifies itself with a User-Agent, and keeps the number of pages small.
  * If PSAL is down or a page can't be parsed, the previous games.json is
    kept untouched. Old results are never deleted.
  * Scores are only recorded when they can be read unambiguously. Otherwise
    the site shows "Awaiting result" instead of guessing.

Commands:
  python sync/psal_sync.py sync     --data games.json   # real sync (writes games.json)
  python sync/psal_sync.py preview  --data games.json   # same, but writes sync/preview/games.json only
  python sync/psal_sync.py probe                        # saves raw pages to sync/debug/ for checking
  python sync/psal_sync.py prepare  --data games.json --previous live.json
  python sync/psal_sync.py import-csv schedule.csv --data games.json   # manual / authorized-feed fallback
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
import os
import re
import sys
import time
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urljoin
from urllib.robotparser import RobotFileParser
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Comment, NavigableString, Tag

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------
TZ = ZoneInfo("America/New_York")
BASE = "https://www.psal.org"
SCHOOL_ID = os.environ.get("PSAL_SCHOOL_ID", "10501")          # Bronx HS of Science
CALENDAR_PATH = "/events-and-standings/school-calendar.aspx"
SPORT_PATH = "/sports/sport.aspx"
DETAIL_PATH = "/games/game-detail.aspx"
CALENDAR_URL = f"{BASE}{CALENDAR_PATH}?schid={SCHOOL_ID}"

CONTACT = os.environ.get("SYNC_CONTACT", "https://github.com")
USER_AGENT = f"BXSCI-Athletics-Sync/1.0 (independent student project; +{CONTACT})"

REQUEST_DELAY = float(os.environ.get("SYNC_REQUEST_DELAY", "2.5"))  # seconds between requests
MONTHS_BACK = 2            # calendar months before the current one (covers the start of a season)
MONTHS_AHEAD = 2           # calendar months after the current one
MAX_DETAIL_PAGES = int(os.environ.get("SYNC_MAX_DETAIL_PAGES", "60"))   # game pages opened per run
GIVE_UP_AFTER_DAYS = 30    # stop re-checking a game this old if PSAL still shows no score...
GIVE_UP_AFTER_CHECKS = 6   # ...after this many checks
SPORT_PAGE_NEAR_DAYS = (-2, 7)          # sport pages fetched every run for games in this range
FULL_REFRESH_HOURS = 6                  # all sport pages in the window at most this often
MISSING_RUNS_BEFORE_CANCEL = 2          # future game must vanish twice before it's marked canceled

SCRIPT_DIR = Path(__file__).resolve().parent
DEBUG_DIR = SCRIPT_DIR / "debug"
PREVIEW_DIR = SCRIPT_DIR / "preview"

EXIT_OK = 0
EXIT_ERROR = 1

log = logging.getLogger("psal-sync")


class SyncError(Exception):
    """Data could not be parsed or validated. Keep old data and alert."""


class AccessDenied(SyncError):
    """robots.txt disallows us, or the server refused (401/403). Stop."""


class SourceUnavailable(Exception):
    """PSAL is down, slow or rate-limiting. Keep old data and try next run."""


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------
MONTH_NAMES = ["January", "February", "March", "April", "May", "June", "July",
               "August", "September", "October", "November", "December"]
MONTH_RE = re.compile(r"\b(" + "|".join(MONTH_NAMES) + r")\s+(\d{4})\b")
GAME_HREF_RE = re.compile(r"game-detail\.aspx#(\d{1,4})/(\d{1,10})", re.I)
POSTBACK_RE = re.compile(r"__doPostBack\(\s*'([^']*)'\s*,\s*'([^']*)'\s*\)")
TEAM_LINE_RE = re.compile(r"^([A-Z][A-Z0-9 &/.'\-]*?)\s+(BOYS|GIRLS|COED|MIXED)\b(.*)$")
BXS_RE = re.compile(r"bronx\s+(?:h\.?\s*s\.?|high\s+school)\s+(?:of\s+)?science|bronx\s+sci(?:ence)?\b", re.I)
TIME_RE = re.compile(r"\b(\d{1,2}):(\d{2})\s*([AaPp])\.?\s*[Mm]\.?")
STATUS_WORDS_RE = re.compile(
    r"^(?:(postponed|cancel+ed|canceled|forfeit(?:ed)?|rescheduled|suspended|tbd)\s*(?:early|late)?\s*)+", re.I)

SPORT_NAMES = {
    "CROSS COUNTRY": "Cross Country", "TABLE TENNIS": "Table Tennis",
    "INDOOR TRACK": "Indoor Track", "OUTDOOR TRACK": "Outdoor Track",
    "FLAG FOOTBALL": "Flag Football",
}


def norm(s: str | None) -> str:
    return " ".join((s or "").split())


def add_months(y: int, m: int, n: int) -> tuple[int, int]:
    idx = y * 12 + (m - 1) + n
    return idx // 12, idx % 12 + 1


def month_bounds(y: int, m: int) -> tuple[date, date]:
    first = date(y, m, 1)
    ny, nm = add_months(y, m, 1)
    return first, date(ny, nm, 1) - timedelta(days=1)


def parse_time(text: str | None) -> str | None:
    """'4:30 PM' / '04:30 pm' -> '16:30'."""
    if not text:
        return None
    m = TIME_RE.search(text)
    if not m:
        return None
    h, mi, ap = int(m.group(1)), int(m.group(2)), m.group(3).lower()
    if not (1 <= h <= 12 and 0 <= mi < 60):
        return None
    h = h % 12 + (12 if ap == "p" else 0)
    return f"{h:02d}:{mi:02d}"


def parse_us_date(text: str) -> date | None:
    m = re.search(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b", text)
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(1)), int(m.group(2)))
    except ValueError:
        return None


def parse_team_line(line: str | None) -> dict | None:
    """'VOLLEYBALL GIRLS JR. VARSITY' -> sport/gender/level (+ round for F/S)."""
    m = TEAM_LINE_RE.match(norm(line).upper()) if line else None
    if not m:
        return None
    sport_raw, gender_raw, rest = m.group(1).strip(), m.group(2), m.group(3).upper()
    sport = SPORT_NAMES.get(sport_raw, sport_raw.title())
    gender = {"BOYS": "Boys", "GIRLS": "Girls"}.get(gender_raw, "Coed")
    is_jv = bool(re.search(r"\b(JV|JR\.?|JUNIOR|F/S|FROSH)\b|F/S", rest))
    out = {"sport": sport, "gender": gender, "level": "JV" if is_jv else "Varsity"}
    if "F/S" in rest:
        out["round"] = "Frosh/Soph"
    return out


def clean_team(name: str | None) -> str | None:
    n = norm(name)
    if not n:
        return None
    n = re.sub(r"^The\s+", "", n)
    n = re.sub(r"\s+High\s+School$", "", n, flags=re.I)
    return n or None


def is_home_venue(venue: str | None) -> bool:
    return bool(venue and BXS_RE.search(venue))


def status_from_words(words: str) -> tuple[str | None, str | None]:
    """Map PSAL status words to (status, note)."""
    w = words.lower()
    if "cancel" in w:
        return "canceled", None
    if "postpon" in w:
        return "postponed", None
    if "suspend" in w:
        return "postponed", "Suspended"
    if "forfeit" in w:
        return "final", None  # resultText "Forfeit" added by caller
    if "reschedul" in w:
        return "scheduled", "Rescheduled"
    return None, None


def game_key(code: str, gid: str) -> str:
    return f"{code}/{gid}"


def detail_url(code: str, gid: str) -> str:
    return f"{BASE}{DETAIL_PATH}#{code}/{gid}"


# ---------------------------------------------------------------------------
# HTTP with robots.txt, delays and conservative error handling
# ---------------------------------------------------------------------------
class Fetcher:
    def __init__(self, delay: float = REQUEST_DELAY, debug_dir: Path | None = DEBUG_DIR):
        self.session = requests.Session()
        self.session.headers.update({
            "User-Agent": USER_AGENT,
            "Accept": "text/html,application/xhtml+xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.8",
        })
        self.delay = delay
        self.debug_dir = debug_dir
        self.robots: RobotFileParser | None = None
        self.requests_made = 0
        self._last = 0.0

    # -- politeness -------------------------------------------------------
    def wait(self) -> None:
        gap = time.monotonic() - self._last
        if gap < self.delay:
            time.sleep(self.delay - gap)
        self._last = time.monotonic()

    def check_robots(self) -> None:
        r = self._request("GET", f"{BASE}/robots.txt", check_robots=False)
        rp = RobotFileParser()
        if r.status_code in (404, 410):
            rp.parse([])                      # no robots.txt -> no restrictions declared
        elif r.status_code == 200:
            rp.parse(r.text.splitlines())
        else:
            raise SourceUnavailable(f"robots.txt returned HTTP {r.status_code}")
        self.robots = rp
        cd = rp.crawl_delay(USER_AGENT)
        if cd:
            self.delay = max(self.delay, float(cd))
        log.info("robots.txt checked (HTTP %s); delay between requests: %.1fs", r.status_code, self.delay)

    def allowed(self, url: str) -> bool:
        if self.robots is None:
            raise RuntimeError("check_robots() must run first")
        return self.robots.can_fetch(USER_AGENT, url)

    def require_allowed(self, url: str) -> None:
        if not self.allowed(url):
            raise AccessDenied(f"robots.txt disallows {url} — not collecting it.")

    # -- requests ---------------------------------------------------------
    def _request(self, method: str, url: str, check_robots: bool = True, **kw) -> requests.Response:
        if check_robots:
            self.require_allowed(url)
        last_err: Exception | None = None
        for attempt in range(3):
            self.wait()
            try:
                r = self.session.request(method, url, timeout=30, **kw)
                self.requests_made += 1
            except (requests.ConnectionError, requests.Timeout) as e:
                last_err = e
                log.warning("Network problem on %s (attempt %d): %s", url, attempt + 1, e)
                time.sleep(5 * (attempt + 1))
                continue
            if r.status_code == 429:
                raise SourceUnavailable("PSAL asked us to slow down (HTTP 429). Stopping until next run.")
            if r.status_code in (401, 403):
                raise AccessDenied(f"PSAL refused access to {url} (HTTP {r.status_code}). Not retrying.")
            if r.status_code >= 500:
                last_err = SourceUnavailable(f"HTTP {r.status_code}")
                log.warning("PSAL server error %s on %s (attempt %d)", r.status_code, url, attempt + 1)
                time.sleep(5 * (attempt + 1))
                continue
            return r
        raise SourceUnavailable(f"Could not reach {url}: {last_err}")

    def get_html(self, url: str, name: str) -> str:
        r = self._request("GET", url)
        if r.status_code != 200:
            raise SyncError(f"Unexpected HTTP {r.status_code} for {url}")
        self._save(name, r.text)
        return r.text

    def post_html(self, url: str, data: dict, name: str) -> str:
        r = self._request("POST", url, data=data, headers={"Referer": url})
        if r.status_code != 200:
            raise SyncError(f"Unexpected HTTP {r.status_code} for POST {url}")
        self._save(name, r.text)
        return r.text

    def _save(self, name: str, text: str) -> None:
        if self.debug_dir:
            self.debug_dir.mkdir(parents=True, exist_ok=True)
            (self.debug_dir / f"{name}.html").write_text(text, encoding="utf-8")


# ---------------------------------------------------------------------------
# 1. School Sports Calendar
# ---------------------------------------------------------------------------
def calendar_month(soup: BeautifulSoup) -> tuple[int, int] | None:
    """Find the 'November 2026' header that sits between the << >> controls."""
    fallback = None
    for s in soup.find_all(string=MONTH_RE):
        m = MONTH_RE.search(str(s))
        ym = (int(m.group(2)), MONTH_NAMES.index(m.group(1)) + 1)
        holder = s.find_parent(["tr", "table", "div", "span", "td"]) or s.parent
        ctx = holder.get_text(" ", strip=True) if holder else ""
        if any(sym in ctx for sym in ("<<", ">>", "«", "»")):
            return ym
        fallback = fallback or ym
    return fallback


def _lowest_common_ancestor(nodes: list[Tag]) -> Tag | None:
    if not nodes:
        return None
    total = len(nodes)
    for anc in nodes[0].parents:
        if isinstance(anc, Tag) and len(anc.find_all("a", href=GAME_HREF_RE)) >= total:
            return anc
    return None


def parse_calendar(html: str) -> tuple[tuple[int, int] | None, list[dict], list[str]]:
    """Return ((year, month), entries, warnings) for one calendar page."""
    soup = BeautifulSoup(html, "html.parser")
    ym = calendar_month(soup)
    warnings: list[str] = []
    links = soup.find_all("a", href=GAME_HREF_RE)
    if ym is None or not links:
        return ym, [], warnings
    container = _lowest_common_ancestor(links) or soup

    entries: list[dict] = []
    day = prev_day = phase = None
    team_line = None
    for node in container.descendants:
        if isinstance(node, Comment):
            continue
        if isinstance(node, NavigableString):
            parent_a = node.find_parent("a")
            if parent_a is not None and GAME_HREF_RE.search(parent_a.get("href", "")):
                continue  # game link text is handled at the <a>
            t = norm(str(node))
            if not t:
                continue
            if re.fullmatch(r"\d{1,2}", t) and 1 <= int(t) <= 31:
                d = int(t)
                if phase is None:
                    phase = -1 if d > 7 else 0      # leading days from the previous month
                elif prev_day is not None and d < prev_day:
                    phase += 1                       # wrapped into the next month
                day, prev_day, team_line = d, d, None
                continue
            if TEAM_LINE_RE.match(t.upper()) and t == t.upper():
                team_line = t
        elif isinstance(node, Tag) and node.name == "a":
            m = GAME_HREF_RE.search(node.get("href", ""))
            if not m:
                continue
            if day is None:
                warnings.append(f"Game link without a day: {node.get_text(' ', strip=True)}")
                continue
            y, mo = add_months(ym[0], ym[1], phase or 0)
            try:
                d = date(y, mo, day)
            except ValueError:
                warnings.append(f"Invalid date {y}-{mo}-{day}")
                continue
            entries.append({
                "code": m.group(1), "gid": m.group(2), "date": d.isoformat(),
                "teamLine": team_line, "text": norm(node.get_text(" ", strip=True)),
            })
    return ym, entries, warnings


def parse_link_text(text: str) -> dict:
    """'Vs. Seward Park Campus @ Bronx HS Science - Gym 2' or 'City Championships @ Van Cortlandt Park'."""
    t = norm(text)
    head, venue = (t.split(" @", 1) + [""])[:2] if " @" in t else (t, "")
    venue = norm(venue.lstrip("@ ")) or None
    m = re.match(r"^(?:vs\.?|versus)\s+(.*)$", head, re.I)
    if m:
        return {"opponent": clean_team(m.group(1)), "event": None, "location": venue}
    m = re.match(r"^(?:at|@)\s+(.*)$", head, re.I)
    if m:
        return {"opponent": clean_team(m.group(1)), "event": None, "location": venue, "awayHint": True}
    return {"opponent": None, "event": head or None, "location": venue}


def calendar_entry_to_game(e: dict, teams: dict | None = None) -> dict:
    """Never returns None: a game without a sport/team line is kept (classified from its team code if known)."""
    team = parse_team_line(e.get("teamLine")) or dict((teams or {}).get(e["code"]) or {}) or {}
    team = {"sport": team.get("sport") or "Other", "gender": team.get("gender") or "Coed",
            "level": team.get("level") or "Varsity", "round": team.get("round")}
    link = parse_link_text(e["text"])
    if link["opponent"]:
        home_away = "away" if link.get("awayHint") else ("home" if is_home_venue(link["location"]) else "away")
    else:
        home_away = "home" if is_home_venue(link["location"]) else "neutral"
    return {
        "id": f"psal-{e['code']}-{e['gid']}",
        "date": e["date"],
        "time": None,
        "sport": team["sport"],
        "gender": team["gender"],
        "level": team["level"],
        "opponent": link["opponent"],
        "event": link["event"],
        "round": team.get("round") or playoff_label(e["text"]),
        "homeAway": home_away,
        "location": link["location"] or "Location TBA",
        "status": "scheduled",
        "score": None,
        "resultText": None,
        "statusNote": None,
        "psal": {"sportCode": e["code"], "gameId": e["gid"], "url": detail_url(e["code"], e["gid"]),
                 "teamCode": e["code"], "sources": ["calendar"]},
    }


def find_nav(soup: BeautifulSoup, direction: str, page_url: str):
    words = {"next": (">>", "»", ">", "›", "next"), "prev": ("<<", "«", "<", "‹", "prev", "previous")}[direction]
    for a in soup.find_all("a"):
        txt = a.get_text(strip=True).lower()
        title = (a.get("title") or "").lower()
        if txt in words or (title and words[-1] in title) or (direction == "prev" and "previous" in title):
            href = a.get("href", "")
            m = POSTBACK_RE.search(href)
            if m:
                return ("postback", m.group(1), m.group(2))
            if href and not href.startswith(("#", "javascript")):
                return ("get", urljoin(page_url, href))
    return None


def form_payload(soup: BeautifulSoup, target: str, argument: str) -> tuple[str, dict]:
    form = soup.find("form")
    action = urljoin(CALENDAR_URL, form.get("action")) if form and form.get("action") else CALENDAR_URL
    data: dict[str, str] = {}
    scope = form or soup
    for inp in scope.find_all("input"):
        name = inp.get("name")
        if not name or inp.get("type", "text").lower() in ("submit", "button", "image", "checkbox", "radio", "file"):
            continue
        data[name] = inp.get("value", "")
    for sel in scope.find_all("select"):
        name = sel.get("name")
        opt = sel.find("option", selected=True) or sel.find("option")
        if name and opt is not None:
            data[name] = opt.get("value", opt.get_text(strip=True))
    data["__EVENTTARGET"] = target
    data["__EVENTARGUMENT"] = argument
    return action, data


def fetch_calendar(fetcher: Fetcher, today: date) -> tuple[dict, list[str]]:
    """Fetch current month, then step back/forward with the << >> controls."""
    warnings: list[str] = []
    html = fetcher.get_html(CALENDAR_URL, "calendar_current")
    ym, entries, w = parse_calendar(html)
    warnings += w
    if ym is None:
        raise SyncError("Could not find the month header on the PSAL school calendar — page layout may have changed.")
    pages = {ym: (html, entries)}

    def walk(direction: str, steps: int):
        cur_html, cur_ym = html, ym
        for i in range(steps):
            soup = BeautifulSoup(cur_html, "html.parser")
            nav = find_nav(soup, direction, CALENDAR_URL)
            if not nav:
                warnings.append(f"No '{direction}' month control found; covering fewer months.")
                return
            name = f"calendar_{direction}{i + 1}"
            if nav[0] == "postback":
                action, data = form_payload(soup, nav[1], nav[2])
                new_html = fetcher.post_html(action, data, name)
            else:
                new_html = fetcher.get_html(nav[1], name)
            new_ym, new_entries, w2 = parse_calendar(new_html)
            expected = add_months(cur_ym[0], cur_ym[1], 1 if direction == "next" else -1)
            if new_ym != expected:
                warnings.append(f"Month navigation returned {new_ym}, expected {expected}; stopped.")
                return
            warnings.extend(w2)
            pages[new_ym] = (new_html, new_entries)
            cur_html, cur_ym = new_html, new_ym

    walk("prev", MONTHS_BACK)
    walk("next", MONTHS_AHEAD)
    if (today.year, today.month) not in pages:
        warnings.append(f"PSAL calendar did not include the current month ({today:%B %Y}).")
    return pages, warnings


# ---------------------------------------------------------------------------
# 2. Sport pages (time, home team, status words)
# ---------------------------------------------------------------------------
SPORT_EVENT_RE = re.compile(
    r"(?P<team>[A-Z][A-Z &/.'\-]*?\s+(?:BOYS|GIRLS|COED|MIXED)\b[A-Z/.\s]*?)\s+"
    r"(?P<date>\d{1,2}/\d{1,2}/\d{4})(?:\s+\d{1,2}:\d{2}:\d{2}\s*[AP]M)?\s+"
    r"(?:(?P<time>\d{1,2}:\d{2}\s*[AaPp]\.?[Mm]\.?)\s+)?"
    r"(?P<rest>.*?)\s+@\s*(?P<venue>.*?)\s*(?:\bmap\b.*)?$")


def parse_sport_page(html: str) -> dict[str, dict]:
    """Return {code/gid: info} for Bronx Science events on a sport page."""
    soup = BeautifulSoup(html, "html.parser")
    found: dict[str, dict] = {}
    for a in soup.find_all("a", href=GAME_HREF_RE):
        m = GAME_HREF_RE.search(a["href"])
        key = game_key(m.group(1), m.group(2))
        node, text = a, ""
        for _ in range(6):
            node = node.parent
            if node is None or len(node.find_all("a", href=GAME_HREF_RE)) > 1:
                node = None
                break
            text = norm(node.get_text(" ", strip=True))
            if re.search(r"\d{1,2}/\d{1,2}/\d{4}", text) and " @" in text:
                break
        if node is None or not text:
            continue
        em = SPORT_EVENT_RE.search(text)
        if not em or not BXS_RE.search(em.group("rest")):
            continue
        rest = em.group("rest")
        status_words = ""
        sw = STATUS_WORDS_RE.match(rest)
        if sw:
            status_words, rest = sw.group(0), rest[sw.end():]
        parts = re.split(r"\s+vs\.?\s+", rest, maxsplit=1, flags=re.I)
        team = parse_team_line(em.group("team")) or {}
        info: dict[str, Any] = {
            "code": m.group(1), "gid": m.group(2), "teamCode": m.group(1),
            "sport": team.get("sport"), "gender": team.get("gender"), "level": team.get("level"),
            "round": team.get("round") or playoff_label(text),
            "date": (parse_us_date(em.group("date")) or date.min).isoformat(),
            "time": parse_time(em.group("time")),
            "location": norm(em.group("venue")) or None,
            "statusWords": norm(status_words) or None,
        }
        if len(parts) == 2:
            home, away = norm(parts[0]), norm(parts[1])
            if BXS_RE.search(home):
                info.update(homeAway="home", opponent=clean_team(away))
            elif BXS_RE.search(away):
                info.update(homeAway="away", opponent=clean_team(home))
        found[key] = info
    return found


# ---------------------------------------------------------------------------
# 3. Game detail pages (rendered with Playwright)
# ---------------------------------------------------------------------------
DETAIL_LABELS = ["sport", "game/meet", "status", "date", "time", "address", "referees",
                 "head coaches", "game/meet results", "athletic trainer", "location", "venue"]

VENUE_WORDS_RE = re.compile(
    r"\b(gym|field|pool|park|court|courts|lanes|stadium|track|oval|campus\s*-|st\.?|street|ave|avenue|"
    r"blvd|road|rd|room|natatorium|map|parade\s+grounds)\b", re.I)
NUM_RE = r"\d{1,3}(?:\.\d{1,2})?"
ROW_RE = re.compile(r"^(?P<name>[^\d]*?[A-Za-z][^\d]*?)\s*[|:\-–]?\s*(?P<nums>" + NUM_RE + r"(?:\s+" + NUM_RE + r")*)$")
NUM_ONLY_RE = re.compile(r"^(" + NUM_RE + r")$")
TOTAL_WORDS = {"total", "totals", "final", "t", "f", "ttl", "score", "pts", "points", "r", "runs", "goals"}
SET_SPORTS = {"Volleyball", "Tennis", "Badminton", "Table Tennis", "Handball"}
STOP_WORDS = {"high", "school", "campus", "educational", "academy", "the", "and", "for", "of", "complex",
              "secondary", "international", "intl", "collegiate"}
ABBREV = {"bklyn": "brooklyn", "tech": "technical", "intl": "international", "hs": "high", "acad": "academy",
          "ctr": "center", "cen": "center", "col": "college", "qns": "queens", "mlk": "king", "st": "saint"}


def _num(s: str) -> int | float:
    v = float(s)
    return int(v) if v.is_integer() else v


def _words(name: str) -> set[str]:
    out = set()
    for w in re.findall(r"[a-z]+", name.lower()):
        w = ABBREV.get(w, w)
        if len(w) >= 4 and w not in STOP_WORDS:
            out.add(w)
    return out


def _name_matches(a: str, b: str | None) -> bool:
    """Loose team-name match: share a distinctive word ('Bklyn Tech.' ~ 'Brooklyn Technical')."""
    if not b:
        return False
    wa, wb = _words(a), _words(b)
    if wa & wb:
        return True
    # prefix match handles 'Tech' vs 'Technical', 'Stuy' vs 'Stuyvesant'
    return any(x.startswith(y) or y.startswith(x) for x in wa for y in wb if min(len(x), len(y)) >= 4)


def classify_status(text: str | None) -> str:
    """'final' | 'notplayed' | 'off' (postponed/canceled) | 'unknown'."""
    t = (text or "").lower()
    if any(w in t for w in ("postpon", "cancel", "suspend")):
        return "off"
    if "not played" in t or "unplayed" in t:
        return "notplayed"
    if any(w in t for w in ("final", "complete", "played", "official", "forfeit", "result")):
        return "final"
    if any(w in t for w in ("scheduled", "pending", "tbd", "tba", "upcoming", "active", "not started")):
        return "notplayed"
    return "unknown"


def extract_score(lines: list[str], skip: set[int], opponent: str | None, sport: str | None) -> tuple[dict | None, str]:
    """
    Find Bronx Science's and the opponent's final score in the visible text of a game page.
    Supports these layouts (one line each unless noted):
      A  "The Bronx High School of Science 3"   +  "East Harlem Pride 1"   (also tab-separated rows)
      B  "The Bronx High School of Science"  /  "3"  /  "East Harlem Pride"  /  "1"   (name and score on separate lines)
      C  "Bronx Science 3 - 1 East Harlem"   |   "Bronx Science 3, East Harlem 1"   |   "Bronx Science vs East Harlem 3-1"
      D  box score with a total column:  "Team 1 2 3 4 Total" / "Bronx Science 10 12 8 15 45" / "Lehman 8 9 10 11 38"
      E  sets without a total (volleyball, tennis…):  "Set 1 Set 2 Set 3" / "Bronx Science 25 25 25" / "Opp 20 18 22"  -> 3-0
    Decimal scores (gymnastics 110.00 / 98.35) are kept as numbers.
    Returns (score or None, reason).
    """
    rows: list[tuple[int, str, list]] = []          # (line index, name, numbers)
    for i, l in enumerate(lines):
        if i in skip or VENUE_WORDS_RE.search(l):
            continue
        m = ROW_RE.match(l)
        if m and len(re.findall(r"[A-Za-z]", m.group("name"))) >= 3:
            rows.append((i, m.group("name").strip(" -|:"), [_num(x) for x in m.group("nums").split()]))
    # B: name-only line followed by a number-only line
    for i in range(len(lines) - 1):
        if i in skip or i + 1 in skip or VENUE_WORDS_RE.search(lines[i]):
            continue
        if re.search(r"\d", lines[i]) or len(re.findall(r"[A-Za-z]", lines[i])) < 3:
            continue
        m = NUM_ONLY_RE.match(lines[i + 1])
        if m and (BXS_RE.search(lines[i]) or _name_matches(lines[i], opponent)):
            rows.append((i, lines[i], [_num(m.group(1))]))

    mismatched: list[str] = []

    def pick(bx_rows, opp_rows):
        if opponent:   # the other team on the page must be this game's opponent
            mismatched.extend(r[1] for r in opp_rows if not _name_matches(r[1], opponent))
            opp_rows = [r for r in opp_rows if _name_matches(r[1], opponent)]
        if len(bx_rows) == 1 and len(opp_rows) == 1:
            return bx_rows[0], opp_rows[0]
        return None

    found: list[tuple[str, dict]] = []

    # D / E: multi-number rows
    multi = [r for r in rows if len(r[2]) >= 2]
    bx_m = [r for r in multi if BXS_RE.search(r[1])]
    op_m = [r for r in multi if not BXS_RE.search(r[1])]
    pr = pick(bx_m, op_m)
    if pr and len(pr[0][2]) == len(pr[1][2]):
        (bi, _, bn), (oi, _, on) = pr
        header = next((lines[j] for j in range(min(bi, oi) - 1, max(-1, min(bi, oi) - 4), -1)
                       if j not in skip and re.search(r"[A-Za-z]", lines[j])), "")
        tokens = header.split()
        trailing = [t.lower().strip(".:") for t in tokens[-len(bn):]] if len(tokens) >= len(bn) else []
        tot_idx = next((k for k, t in enumerate(trailing) if t in TOTAL_WORDS), None)
        if tot_idx is not None:
            found.append(("D", {"bxsci": bn[tot_idx], "opponent": on[tot_idx]}))
        elif sport in SET_SPORTS and re.search(r"\b(set|game|g)\s*\d", header, re.I) and all(x <= 50 for x in bn + on):
            won = sum(1 for a, b in zip(bn, on) if a > b)
            lost = sum(1 for a, b in zip(bn, on) if b > a)
            found.append(("E", {"bxsci": won, "opponent": lost}))

    # A / B: single-number rows
    single = [r for r in rows if len(r[2]) == 1]
    pr = pick([r for r in single if BXS_RE.search(r[1])], [r for r in single if not BXS_RE.search(r[1])])
    if pr:
        found.append(("A", {"bxsci": pr[0][2][0], "opponent": pr[1][2][0]}))

    # C: both teams on one line
    for i, l in enumerate(lines):
        if i in skip or not BXS_RE.search(l):
            continue
        pats = [
            (r"^(?P<a>.+?)\s+(?P<x>" + NUM_RE + r")\s*[-–:]\s*(?P<y>" + NUM_RE + r")\s+(?P<b>.+)$", False),
            (r"^(?P<a>.+?)\s+(?P<x>" + NUM_RE + r")\s*[,;]\s*(?P<b>.+?)\s+(?P<y>" + NUM_RE + r")$", False),
            (r"^(?P<a>.+?)\s+(?:vs\.?|v\.?|at|@)\s+(?P<b>.+?)[\s:,]+(?P<x>" + NUM_RE + r")\s*[-–:]\s*(?P<y>" + NUM_RE + r")$", False),
        ]
        for pat, _ in pats:
            m = re.match(pat, l, re.I)
            if not m:
                continue
            a, b, x, y = m.group("a"), m.group("b"), _num(m.group("x")), _num(m.group("y"))
            other = b if BXS_RE.search(a) else a
            if opponent and not _name_matches(other, opponent):
                mismatched.append(other)
                break
            if BXS_RE.search(a) and not BXS_RE.search(b):
                found.append(("C", {"bxsci": x, "opponent": y}))
            elif BXS_RE.search(b) and not BXS_RE.search(a):
                found.append(("C", {"bxsci": y, "opponent": x}))
            break

    if not found:
        if mismatched:
            return None, f"other team on page ({mismatched[0]!r}) doesn't match scheduled opponent {opponent!r}"
        return None, "no score lines for both teams were found on the page"
    distinct = {json.dumps(sc, sort_keys=True) for _, sc in found}
    if len(distinct) > 1:
        return None, "page shows conflicting numbers: " + "; ".join(f"{k}={v}" for k, v in found)
    return found[0][1], f"layout {found[0][0]}"


def parse_detail_text(text: str, opponent: str | None = None, sport: str | None = None) -> dict:
    """Read status/time and, only when unambiguous, the final score from a rendered detail page."""
    lines = [norm(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
    low = [l.lower().rstrip(":") for l in lines]
    skip: set[int] = {i for i, l in enumerate(low) if l in DETAIL_LABELS}

    def value(label: str) -> str | None:
        for i, l in enumerate(low):
            if l == label:
                for j in range(i + 1, min(i + 3, len(lines))):
                    if low[j] not in DETAIL_LABELS:
                        skip.add(j)
                        return lines[j]
                return None
            if l.startswith(label + ":"):
                skip.add(i)
                return lines[i].split(":", 1)[1].strip() or None
            # Table rows render as "Label<TAB>Value" -> "Label Value" on one line.
            if l.startswith(label + " "):
                rest = lines[i][len(label):].strip()
                if rest.lower() not in DETAIL_LABELS and not any(
                        l.startswith(other) for other in DETAIL_LABELS if len(other) > len(label)):
                    skip.add(i)
                    return rest or None
        return None

    status = value("status")
    out: dict[str, Any] = {"status": status, "time": parse_time(value("time") or ""), "score": None}
    for lab in ("sport", "game/meet", "date", "address", "location", "venue", "referees", "head coaches",
                "athletic trainer"):
        value(lab)
    # Date / time-looking lines are never scores.
    for i, l in enumerate(lines):
        if re.search(r"\d{1,2}/\d{1,2}/\d{2,4}|\d{1,2}:\d{2}", l):
            skip.add(i)

    state = classify_status(status)
    score, reason = extract_score(lines, skip, opponent, sport)
    if score is not None:
        if state in ("off", "notplayed"):
            score, reason = None, f"numbers found but PSAL status is '{status}' (not a final result)"
        elif state == "unknown" and score["bxsci"] == 0 and score["opponent"] == 0:
            score, reason = None, "0–0 shown without a final status: treated as not yet published"
    out["score"] = score
    out["statusClass"] = state
    out["scoreReason"] = reason if score is None else f"read ({reason})"
    return out


def fetch_details(fetcher: Fetcher, targets: list[tuple], debug_dir: Path | None,
                  capture_network: bool = False) -> tuple[dict, list[str], list[str]]:
    """Render game-detail pages. targets: (code, gid, opponent, sport). Returns (results, warnings, network_urls)."""
    results: dict[str, dict] = {}
    warnings: list[str] = []
    network: list[str] = []
    if not targets:
        return results, warnings, network
    fetcher.require_allowed(f"{BASE}{DETAIL_PATH}")
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        warnings.append("Playwright is not installed — scores from game-detail pages were skipped.")
        return results, warnings, network

    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=USER_AGENT, timezone_id="America/New_York", locale="en-US")
        # Don't download images, fonts or media — lighter on PSAL's servers.
        ctx.route("**/*", lambda route: route.abort()
                  if route.request.resource_type in ("image", "media", "font") else route.continue_())
        page = ctx.new_page()
        if capture_network:
            page.on("request", lambda r: network.append(f"{r.method} {r.url}")
                    if r.resource_type in ("xhr", "fetch", "script", "document") else None)
        for t in targets:
            code, gid, opponent = t[0], t[1], t[2]
            sport = t[3] if len(t) > 3 else None
            url = detail_url(code, gid)
            fetcher.wait()
            try:
                page.goto("about:blank")
                resp = page.goto(url, wait_until="networkidle", timeout=45000)
                fetcher.requests_made += 1
                if resp is not None and resp.status in (401, 403, 429):
                    warnings.append(f"PSAL returned HTTP {resp.status} for game pages; stopped checking scores.")
                    break
                try:
                    page.wait_for_function(
                        "() => /status/i.test(document.body.innerText) && /\\d{1,2}:\\d{2}/.test(document.body.innerText)",
                        timeout=8000)
                except Exception:
                    pass
                page.wait_for_timeout(700)   # results can arrive a moment after the header fields
                parts = [page.inner_text("body")]
                for fr in page.frames[1:]:   # results shown inside an embedded frame are included too
                    try:
                        parts.append(fr.inner_text("body"))
                    except Exception:
                        pass
                text = "\n".join(parts)
            except Exception as e:  # one bad page shouldn't sink the whole run
                warnings.append(f"Could not render {url}: {e}")
                continue
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"detail_{code}_{gid}.txt").write_text(text, encoding="utf-8")
            results[game_key(code, gid)] = parse_detail_text(text, opponent, sport)
        browser.close()
    return results, warnings, network


def apply_detail(g: dict, d: dict) -> None:
    status_text = (d.get("status") or "").lower()
    if d.get("time") and not g.get("time"):
        g["time"] = d["time"]
    if "cancel" in status_text:
        g["status"], g["score"] = "canceled", None
    elif "postpon" in status_text or "suspend" in status_text:
        g["status"], g["score"] = "postponed", None
    elif "forfeit" in status_text:
        g["status"], g["resultText"] = "final", g.get("resultText") or "Forfeit"
    if d.get("score") is not None and g["status"] not in ("canceled", "postponed"):
        g["score"] = d["score"]
        g["status"] = "final"


# ---------------------------------------------------------------------------
# 4. Team discovery + team FULL SCHEDULE pages (cross-check source)
# ---------------------------------------------------------------------------
# The school calendar is the primary list of games, but on psal.org it does not show
# every Bronx Science game (Nov 2026 showed 19 events; JV volleyball, girls cross
# country, table tennis and playoff games were absent). Each Bronx Science team has a
# profile page  team-profile.aspx#<team code>/10501  whose FULL SCHEDULE tab lists the
# team's season. Team codes are discovered from the team directory on every sport page.
TEAM_PROFILE_PATH = "/profiles/team-profile.aspx"
TEAM_PROFILE_RE = re.compile(r"team-profile\.aspx#(\d{1,4})/(\d{4,6})", re.I)
SPORT_NAV_RE = re.compile(r"sport\.aspx\?spCode=(\d{3})&(?:amp;)?flag=All", re.I)
SPORT_HEADING_RE = re.compile(r"^([A-Z][A-Z &/.'\-]+?)\s+(\d{3})$")
PLAYOFF_RE = re.compile(
    r"\b(play-?offs?|round of \d+|(?:first|second|third|1st|2nd|3rd) round|quarter-?finals?|semi-?finals?|"
    r"championships?|city finals?|crossover|wild ?card)\b", re.I)
RESULT_RE = re.compile(r"(?<![A-Za-z])([WLT])(?![A-Za-z])\s*[,:(]?\s*(\d{1,3}(?:\.\d{1,2})?)\s*[-–]\s*(\d{1,3}(?:\.\d{1,2})?)")
RESULT_AFTER_RE = re.compile(r"(\d{1,3}(?:\.\d{1,2})?)\s*[-–]\s*(\d{1,3}(?:\.\d{1,2})?)\s*\(?([WLT])\)?(?![A-Za-z])")
DATE_MD_RE = re.compile(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b")
TEAM_REFRESH_HOURS = 3          # in-season teams: re-read the full schedule this often
OFFSEASON_REFRESH_HOURS = 24    # other teams: once a day (finds teams missing from the calendar)
DISCOVERY_REFRESH_HOURS = 24    # re-scan sport pages for Bronx Science teams once a day
MAX_TEAM_PAGES = int(os.environ.get("SYNC_MAX_TEAM_PAGES", "25"))


def team_profile_url(code: str) -> str:
    return f"{BASE}{TEAM_PROFILE_PATH}#{code}/{SCHOOL_ID}"


def team_info(sport: str | None, type_text: str) -> dict:
    t = (type_text or "").lower()
    gender = "Boys" if re.search(r"\bboys?\b", t) else "Girls" if re.search(r"\bgirls?\b", t) else "Coed"
    level = "JV" if re.search(r"\bjv\b|\bjr\b|junior|f/s|frosh", t) else "Varsity"
    return {"sport": sport, "gender": gender, "level": level, "type": norm(type_text)}


def parse_sport_nav(html: str) -> list[str]:
    """Sport page codes linked from PSAL's navigation (sport.aspx?spCode=NNN&flag=All)."""
    codes: list[str] = []
    for m in SPORT_NAV_RE.finditer(html):
        if m.group(1) not in codes:
            codes.append(m.group(1))
    return codes


def parse_team_directory(html: str) -> dict[str, dict]:
    """Bronx Science rows in a sport page's team directory -> {team code: info}."""
    soup = BeautifulSoup(html, "html.parser")
    sport = None
    for s in soup.find_all(string=SPORT_HEADING_RE):
        m = SPORT_HEADING_RE.match(norm(str(s)))
        if m:
            sport = SPORT_NAMES.get(m.group(1).strip(), m.group(1).strip().title())
            break
    out: dict[str, dict] = {}
    for a in soup.find_all("a", href=TEAM_PROFILE_RE):
        m = TEAM_PROFILE_RE.search(a["href"])
        if m.group(2) != SCHOOL_ID:
            continue
        row = a.find_parent("tr")
        cells = [norm(td.get_text(" ", strip=True)) for td in row.find_all(["td", "th"])] if row else []
        type_text = next((c for c in cells if re.search(r"\b(boys|girls|coed|mixed)\b", c, re.I)), "")
        info = team_info(sport, type_text)
        info["division"] = next((c for c in cells if "--" in c or re.search(r"division|bronx|manhattan|queens|brooklyn", c, re.I)
                                 and c not in (type_text,)), None)
        out[m.group(1)] = info
    return out


# JavaScript run inside the rendered team profile: every table row (and every game link's
# surrounding block) that contains a date, with its cell texts and link targets.
TEAM_ROWS_JS = r"""() => {
  const out = [], seen = new Set();
  const docs = [document];
  for (const f of document.querySelectorAll('iframe')) { try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} }
  const hasDate = t => /\b\d{1,2}\/\d{1,2}\b/.test(t || '');
  const visible = el => !!(el.offsetWidth || el.offsetHeight || el.getClientRects().length);
  for (const d of docs) {
    for (const tr of d.querySelectorAll('tr')) {
      if (!visible(tr)) continue;
      const cells = Array.from(tr.querySelectorAll('td,th')).map(c => (c.innerText || '').trim());
      if (!hasDate(cells.join(' '))) continue;
      const links = Array.from(tr.querySelectorAll('a[href]')).map(a => a.getAttribute('href'));
      links.forEach(h => seen.add(h));
      out.push({cells, links, section: (tr.closest('[id]') || {}).id || ''});
    }
    for (const a of d.querySelectorAll('a[href*="game-detail"]')) {
      const h = a.getAttribute('href');
      if (seen.has(h) || !visible(a)) continue;
      let el = a;
      for (let i = 0; i < 5 && el.parentElement; i++) { el = el.parentElement; if (hasDate(el.innerText)) break; }
      out.push({cells: [(el.innerText || '').replace(/\s+/g, ' ').trim()], links: [h], section: (el.closest('[id]') || {}).id || ''});
      seen.add(h);
    }
  }
  return out;
}"""


def fetch_team_schedules(fetcher: Fetcher, codes: list[str], debug_dir: Path | None) -> tuple[dict, list[str]]:
    """Render team profile FULL SCHEDULE tabs. Returns ({code: rows or None if unreadable}, warnings)."""
    results: dict[str, list | None] = {}
    warnings: list[str] = []
    if not codes:
        return results, warnings
    if not fetcher.allowed(f"{BASE}{TEAM_PROFILE_PATH}"):
        warnings.append("robots.txt disallows team profile pages — cross-check against team schedules skipped.")
        return {c: None for c in codes}, warnings
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        warnings.append("Playwright is not installed — team schedules were skipped.")
        return {c: None for c in codes}, warnings
    with sync_playwright() as p:
        browser = p.chromium.launch()
        ctx = browser.new_context(user_agent=USER_AGENT, timezone_id="America/New_York", locale="en-US")
        ctx.route("**/*", lambda route: route.abort()
                  if route.request.resource_type in ("image", "media", "font") else route.continue_())
        page = ctx.new_page()
        for code in codes:
            url = team_profile_url(code)
            fetcher.wait()
            try:
                page.goto("about:blank")
                resp = page.goto(url, wait_until="networkidle", timeout=45000)
                fetcher.requests_made += 1
                if resp is not None and resp.status in (401, 403, 429):
                    warnings.append(f"PSAL returned HTTP {resp.status} for team pages; stopped reading team schedules.")
                    results.update({c: None for c in codes if c not in results})
                    break
                try:   # make sure the FULL SCHEDULE tab is the one showing
                    tab = page.get_by_text("FULL SCHEDULE", exact=True).first
                    if tab.count():
                        tab.click(timeout=3000)
                        page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                page.wait_for_timeout(800)
                rows = page.evaluate(TEAM_ROWS_JS)
                text = page.inner_text("body")
            except Exception as e:
                warnings.append(f"Could not read team schedule {code}: {e}")
                results[code] = None
                continue
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"team_{code}.json").write_text(json.dumps(rows, indent=1), encoding="utf-8")
                (debug_dir / f"team_{code}.txt").write_text(text, encoding="utf-8")
            results[code] = rows
        browser.close()
    return results, warnings


def resolve_md_date(mo: int, d: int, y: int | None, today: date) -> date | None:
    """'11/09' has no year: pick the year that puts the date closest to today."""
    try:
        if y is not None:
            return date(y + 2000 if y < 100 else y, mo, d)
        options = [date(today.year + k, mo, d) for k in (-1, 0, 1)]
    except ValueError:
        return None
    return min(options, key=lambda x: abs((x - today).days))


def result_from_text(text: str) -> dict | None:
    """'W 3-1', 'L 1-3', 'L 3-1', '3-1 W', 'T 0-0' -> Bronx Science-first score. Needs the W/L/T letter."""
    m = RESULT_RE.search(text)
    if m:
        letter, x, y = m.group(1).upper(), _num(m.group(2)), _num(m.group(3))
    else:
        m = RESULT_AFTER_RE.search(text)
        if not m:
            return None
        x, y, letter = _num(m.group(1)), _num(m.group(2)), m.group(3).upper()
    hi, lo = max(x, y), min(x, y)
    if letter == "W" and hi != lo:
        return {"bxsci": hi, "opponent": lo}
    if letter == "L" and hi != lo:
        return {"bxsci": lo, "opponent": hi}
    if letter == "T" and x == y:
        return {"bxsci": x, "opponent": y}
    return None   # contradictory (e.g. "W 2-2"): don't guess


def playoff_label(text: str) -> str | None:
    """'PSAL Playoffs - Round of 32' -> 'Playoffs · Round of 32'; 'City Championships' -> 'Championship'."""
    words = [m.group(1) for m in PLAYOFF_RE.finditer(text or "")]
    if not words:
        return None
    stage = next((w for w in words if not re.match(r"(play-?offs?|championships?)$", w, re.I)), None)
    if stage:
        stage = stage[0].upper() + stage[1:].lower()
        return f"Playoffs · {stage}"
    if any(re.match(r"play-?offs?$", w, re.I) for w in words):
        return "Playoffs"
    return "Championship"


def parse_team_rows(rows: list[dict], code: str, info: dict, today: date) -> tuple[list[dict], int]:
    """Turn rendered FULL SCHEDULE rows into game entries. Returns (entries, rows skipped)."""
    entries: list[dict] = []
    skipped = 0
    for r in rows or []:
        cells = [norm(c) for c in r.get("cells", []) if norm(c)]
        text = " | ".join(cells)
        low = text.lower()
        if not cells or "practice" in low or "today's events" in low:
            skipped += 1
            continue
        dm = DATE_MD_RE.search(text)
        d = resolve_md_date(int(dm.group(1)), int(dm.group(2)), int(dm.group(3)) if dm.group(3) else None, today) if dm else None
        if d is None:
            skipped += 1
            continue
        link = next((GAME_HREF_RE.search(h) for h in r.get("links", []) if GAME_HREF_RE.search(h or "")), None)
        opp = ha = venue = None
        other_teams_row = False
        for c in cells:   # "A vs B" in one cell
            parts = re.split(r"\s+vs\.?\s+", c, maxsplit=1, flags=re.I)
            if len(parts) == 2:
                a, b = norm(parts[0]), norm(parts[1])
                if BXS_RE.search(a):
                    opp, ha = b, "home"
                elif BXS_RE.search(b):
                    opp, ha = a, "away"
                else:
                    other_teams_row = True   # another school's game (e.g. a sidebar)
                break
        if other_teams_row:
            skipped += 1
            continue
        if opp is None:
            for c in cells:
                m = re.match(r"^(vs\.?|versus|@|at)\s+(.+)$", c, re.I)
                if m:
                    opp, ha = m.group(2), ("home" if m.group(1).lower().startswith("v") else "away")
                    break
        if ha is None:
            ha = next(({"h": "home", "home": "home", "a": "away", "away": "away", "n": "neutral", "neutral": "neutral"}[c.lower()]
                       for c in cells if c.lower() in ("h", "a", "home", "away", "n", "neutral")), None)
        if opp is None:
            for c in cells:
                if (len(re.findall(r"[A-Za-z]", c)) >= 3 and not BXS_RE.search(c) and not DATE_MD_RE.search(c)
                        and not TIME_RE.search(c) and not RESULT_RE.search(c) and not VENUE_WORDS_RE.search(c)
                        and not PLAYOFF_RE.search(c) and not STATUS_WORDS_RE.match(c)
                        and c.lower() not in ("home", "away", "neutral", "final", "w", "l", "t", "tba", "tbd")):
                    opp = c
                    break
        if opp and " @ " in f" {opp} ":
            opp, venue = [norm(x) for x in opp.split("@", 1)]
        event = None
        if opp is None:   # meets / championships: "Bronx Borough Championships @ Van Cortlandt Park"
            for c in cells:
                if "@" in c and not c.lstrip().startswith("@"):
                    head, tail = [norm(x) for x in c.split("@", 1)]
                    if len(re.findall(r"[A-Za-z]", head)) >= 3 and not DATE_MD_RE.search(head):
                        event, venue = head, venue or tail or None
                        break
        if venue is None:
            venue = next((c for c in cells if VENUE_WORDS_RE.search(c) and not BXS_RE.fullmatch(c)), None)
        result = result_from_text(text)
        status_words = " ".join(w for w in ("postponed", "cancel", "forfeit", "rescheduled", "suspended") if w in low)
        if not link and not opp and not result and not event:
            skipped += 1          # not a game row
            continue
        entries.append({
            "code": link.group(1) if link else code, "gid": link.group(2) if link else None, "teamCode": code,
            "date": d.isoformat(), "time": parse_time(text), "opponent": clean_team(opp), "homeAway": ha,
            "location": venue, "event": event, "result": result, "statusWords": status_words or None,
            "round": playoff_label(text), "sport": info.get("sport"), "gender": info.get("gender"),
            "level": info.get("level"), "raw": text[:200],
        })
    return entries, skipped


# ---------------------------------------------------------------------------
# Combining sources, duplicates and completeness
# ---------------------------------------------------------------------------
def _slug(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", "-", (s or "event").lower()).strip("-")[:30] or "event"


def same_game(g: dict, e: dict) -> bool:
    """Same date, same team (sport/gender/level) and same opponent -> the same game."""
    if g["date"] != e["date"]:
        return False
    for k in ("sport", "gender", "level"):
        if e.get(k) and g.get(k) and e[k] != g[k]:
            return False
    if g.get("opponent") and e.get("opponent"):
        return _name_matches(g["opponent"], e["opponent"]) or _name_matches(e["opponent"], g["opponent"])
    return not g.get("opponent") and not e.get("opponent")


def new_game_from_entry(e: dict, source: str) -> dict:
    key_gid = e.get("gid")
    gid = f"psal-{e['code']}-{key_gid}" if key_gid else f"psal-{e['teamCode']}-s{e['date'].replace('-', '')}-{_slug(e.get('opponent'))}"
    g = {
        "id": gid, "date": e["date"], "time": e.get("time"), "sport": e.get("sport") or "Other",
        "gender": e.get("gender") or "Coed", "level": e.get("level") or "Varsity",
        "opponent": e.get("opponent"), "event": None if e.get("opponent") else (e.get("event") or None),
        "round": e.get("round"), "homeAway": e.get("homeAway") or ("home" if is_home_venue(e.get("location")) else "neutral" if not e.get("opponent") else "away"),
        "location": e.get("location") or "Location TBA", "status": "scheduled", "score": None,
        "resultText": None, "statusNote": None,
        "psal": {"sportCode": e["code"], "gameId": key_gid, "url": detail_url(e["code"], key_gid) if key_gid else team_profile_url(e["teamCode"]),
                 "teamCode": e.get("teamCode"), "sources": [source]},
    }
    enrich(g, e, source)
    return g


def enrich(g: dict, e: dict, source: str) -> None:
    """Fill gaps in a game from another source. The calendar's own facts are never overwritten."""
    ps_ = g.setdefault("psal", {})
    if source not in ps_.setdefault("sources", []):
        ps_["sources"].append(source)
    if e.get("teamCode") and not ps_.get("teamCode"):
        ps_["teamCode"] = e["teamCode"]
    if e.get("time") and not g.get("time"):
        g["time"] = e["time"]
    if e.get("homeAway") and source in ("team", "sportpage"):
        g["homeAway"] = e["homeAway"]
        ps_["_sportPage"] = True          # authoritative home/away (see merge)
    if e.get("opponent") and not g.get("opponent"):
        g["opponent"], g["event"] = e["opponent"], None
    if e.get("location") and g.get("location") in (None, "", "Location TBA"):
        g["location"] = e["location"]
    if e.get("round") and not g.get("round"):
        g["round"] = e["round"]
    for k in ("sport", "gender", "level"):
        if e.get(k) and g.get(k) in (None, "", "Other"):
            g[k] = e[k]
    if e.get("statusWords"):
        st, note = status_from_words(e["statusWords"])
        if st:
            g["status"] = st
            if "forfeit" in e["statusWords"].lower():
                g["resultText"] = g.get("resultText") or "Forfeit"
            if note:
                g["statusNote"] = note
    if e.get("result") is not None and g.get("score") is None and g["status"] not in ("canceled", "postponed"):
        g["score"], g["status"] = e["result"], "final"
        ps_["scoreSource"] = source


def combine(fresh: dict[str, dict], entries: list[dict], source: str, report: dict) -> int:
    """Add entries from a cross-check source. Returns how many games were new (not on the calendar)."""
    added = 0
    for e in entries:
        key = game_key(e["code"], e["gid"]) if e.get("gid") else None
        if key and key in fresh:
            enrich(fresh[key], e, source)
            continue
        match = None
        for k, g in fresh.items():
            if same_game(g, e):
                # Two different PSAL game ids with different start times on the same day = a doubleheader.
                if key and g["psal"].get("gameId") and g.get("time") and e.get("time") and g["time"] != e["time"]:
                    continue
                match = (k, g)
                break
        if match:
            enrich(match[1], e, source)
            if key:
                aliases = match[1]["psal"].setdefault("aliases", [])
                if key not in aliases:
                    aliases.append(key)
                report["duplicates"].append({"kept": match[1]["id"], "duplicate": key, "source": source,
                                             "date": e["date"], "opponent": e.get("opponent")})
            continue
        g = new_game_from_entry(e, source)
        fresh[key or g["id"]] = g
        added += 1
        report["notOnCalendar"].append({"id": g["id"], "date": g["date"], "team": f"{g['gender']} {g['level']} {g['sport']}",
                                        "opponent": g.get("opponent") or g.get("event"), "source": source,
                                        "round": g.get("round")})
    return added


def new_report() -> dict:
    return {"missingFromCollected": [], "notOnCalendar": [], "duplicates": [], "unclassified": [],
            "teams": {}, "teamSchedulesUnreadable": [], "scoreConflicts": [], "playedWithoutScore": [],
            "unverifiedMissing": [], "perDate": {}}


def completeness(report: dict, cal_entries: dict[str, dict], fresh: dict[str, dict], games: list[dict],
                 details: dict, prev_by_id: dict, now: datetime) -> dict:
    """Compare the school calendar with what was collected, date by date, and explain every played game without a score."""
    today, hm = now.date().isoformat(), now.strftime("%H:%M")
    collected_ids = {g["id"] for g in games}
    alias_to_id = {}
    for g in fresh.values():
        for a in (g.get("psal") or {}).get("aliases", []):
            alias_to_id[a] = g["id"]
    per_date: dict[str, dict] = {}
    for key, e in cal_entries.items():
        gid = f"psal-{e['code']}-{e['gid']}"
        day = per_date.setdefault(e["date"], {"calendar": 0, "collected": 0, "missing": [], "extra": []})
        day["calendar"] += 1
        if gid in collected_ids or alias_to_id.get(key) in collected_ids:
            day["collected"] += 1
        else:
            day["missing"].append(f"{e.get('teamLine') or '?'} {e['text']} (#{key})")
            report["missingFromCollected"].append({"date": e["date"], "calendar": e["text"], "teamLine": e.get("teamLine"), "key": key})
    for item in report["notOnCalendar"]:
        per_date.setdefault(item["date"], {"calendar": 0, "collected": 0, "missing": [], "extra": []})["extra"].append(
            f"{item['team']} vs {item['opponent']} (from {item['source']})")
    report["perDate"] = {d: v for d, v in sorted(per_date.items()) if v["missing"] or v["extra"]}

    for g in games:
        if g.get("score") is not None or g.get("resultText") or g["status"] in ("canceled", "postponed") or not g.get("opponent"):
            continue
        if g["date"] > today or (g["date"] == today and (g.get("time") or "23:59") > hm):
            continue
        key = f"{(g.get('psal') or {}).get('sportCode')}/{(g.get('psal') or {}).get('gameId')}"
        info = g.get("psal") or {}
        if key in details:
            why = details[key].get("scoreReason") or "page checked, no score"
        elif not info.get("gameId"):
            why = "no PSAL game page link for this game (found on a team schedule without one)"
        elif info.get("checks"):
            why = f"PSAL game page checked {info['checks']}x, no score posted yet (last {info.get('checkedAt')})"
        else:
            why = "not checked yet — queued for the next run (per-run page limit)"
        report["playedWithoutScore"].append({"id": g["id"], "date": g["date"], "team": f"{g['gender']} {g['level']} {g['sport']}",
                                             "opponent": g.get("opponent"), "reason": why})
    return report


def report_markdown(report: dict, now: datetime) -> str:
    L = [f"# PSAL completeness report — {now:%b %d, %Y %I:%M %p} ET", ""]
    t = report["teams"]
    L.append(f"- Bronx Science teams known: **{len(t)}** · team schedules read this run: "
             f"{sum(1 for v in t.values() if v.get('readNow'))} · unreadable: {len(report['teamSchedulesUnreadable'])}")
    L.append(f"- Calendar games not collected: **{len(report['missingFromCollected'])}** (should be 0)")
    L.append(f"- Games found only on team schedules / sport pages (not on the school calendar): **{len(report['notOnCalendar'])}**")
    L.append(f"- Duplicates merged: {len(report['duplicates'])} · score conflicts: {len(report['scoreConflicts'])}")
    L.append(f"- Played games still without a published score: **{len(report['playedWithoutScore'])}**")
    if report["unverifiedMissing"]:
        L.append(f"- Games no longer listed but NOT removed (couldn't verify this run): {len(report['unverifiedMissing'])}")
    if report["missingFromCollected"]:
        L += ["", "## ⚠️ Calendar games not collected", *[f"- {m['date']} · {m['teamLine']} · {m['calendar']} (#{m['key']})" for m in report["missingFromCollected"]]]
    if report["perDate"]:
        L += ["", "## Per-date differences (school calendar vs collected)", "| Date | On calendar | Collected from calendar | Missing | Only on other PSAL pages |", "|---|---|---|---|---|"]
        for d, v in report["perDate"].items():
            L.append(f"| {d} | {v['calendar']} | {v['collected']} | {'; '.join(v['missing']) or '—'} | {'; '.join(v['extra']) or '—'} |")
    if report["teamSchedulesUnreadable"]:
        L += ["", "## Team schedules that could not be read", *[f"- {c}: {team_profile_url(c)}" for c in report["teamSchedulesUnreadable"]]]
    if report["unclassified"]:
        L += ["", "## Games kept without a sport/team line", *[f"- {u}" for u in report["unclassified"]]]
    if report["scoreConflicts"]:
        L += ["", "## Score conflicts (game page used)", *[f"- {c['id']}: game page {c['detail']} vs team schedule {c['team']}" for c in report["scoreConflicts"]]]
    if report["playedWithoutScore"]:
        L += ["", "## Played games without a score", "| Date | Team | Opponent | Why |", "|---|---|---|---|"]
        for p in sorted(report["playedWithoutScore"], key=lambda x: x["date"], reverse=True):
            L.append(f"| {p['date']} | {p['team']} | {p['opponent']} | {p['reason']} |")
    if report["teams"]:
        L += ["", "## Bronx Science teams", "| Code | Team | Schedule rows read | Games parsed | Last read |", "|---|---|---|---|---|"]
        for c, v in sorted(report["teams"].items()):
            L.append(f"| {c} | {v.get('gender', '')} {v.get('level', '')} {v.get('sport') or '?'} | {v.get('rows', '—')} | "
                     f"{v.get('games', '—')} | {v.get('readAt', 'never')} |")
    return "\n".join(L)


# ---------------------------------------------------------------------------
# Building, merging and validating
# ---------------------------------------------------------------------------
def build_fresh(pages: dict, teams: dict | None = None) -> tuple[dict[str, dict], dict[str, dict], list[str], list[str]]:
    """Calendar entries -> games. Returns (fresh, calendar entries by key, unclassified, warnings). Nothing is dropped."""
    fresh: dict[str, dict] = {}
    cal_entries: dict[str, dict] = {}
    unclassified: list[str] = []
    for _ym, (_html, entries) in sorted(pages.items()):
        for e in entries:
            key = game_key(e["code"], e["gid"])
            cal_entries[key] = e
            if key in fresh:
                continue                     # same game shown on two month pages (leading/trailing days)
            if not parse_team_line(e.get("teamLine")):
                unclassified.append(f"{e['date']} {e['text']} (#{key})")
            fresh[key] = calendar_entry_to_game(e, teams)
    warnings = [f"{len(unclassified)} calendar games had no sport/team line; kept and classified from their team code."] if unclassified else []
    return fresh, cal_entries, unclassified, warnings


def apply_sport_info(fresh: dict[str, dict], info: dict[str, dict]) -> int:
    n = 0
    for key, i in info.items():
        g = fresh.get(key)
        if not g:
            continue
        n += 1
        g["psal"]["_sportPage"] = True     # internal flag, removed in merge()
        if i.get("time"):
            g["time"] = i["time"]
        if i.get("homeAway"):
            g["homeAway"] = i["homeAway"]
        if i.get("opponent") and not g.get("opponent"):
            g["opponent"] = i["opponent"]
        if i.get("location") and g.get("location") in (None, "Location TBA"):
            g["location"] = i["location"]
        if i.get("statusWords"):
            st, note = status_from_words(i["statusWords"])
            if st:
                g["status"] = st
                if "forfeit" in i["statusWords"].lower():
                    g["resultText"] = "Forfeit"
                if note:
                    g["statusNote"] = note
    return n


def _comparable(games: list[dict]) -> str:
    """Games without bookkeeping fields, for change detection."""
    clean = []
    for g in games:
        g2 = copy.deepcopy(g)
        g2.pop("missingRuns", None)
        if isinstance(g2.get("psal"), dict):
            for k in ("checkedAt", "checks", "sources", "aliases", "_sportPage"):
                g2["psal"].pop(k, None)
        clean.append(g2)
    return json.dumps(sorted(clean, key=lambda x: x.get("id", "")), sort_keys=True)


def merge(previous: dict, fresh: dict[str, dict], window: tuple[date, date], today: date,
          verified_codes: set | None = None, known_codes: set | None = None,
          report: dict | None = None) -> tuple[list[dict], dict]:
    """
    verified_codes: team codes whose FULL SCHEDULE was read successfully this run.
    known_codes:    all Bronx Science team codes we know about.
    A game that PSAL no longer lists is only counted as "missing" when every source that
    should show it was read successfully this run (calendar month + that team's schedule).
    """
    verified_codes = verified_codes or set()
    known_codes = known_codes or set()
    prev_meta = previous.get("meta", {})
    prev_games = previous.get("games", [])
    was_sample = prev_meta.get("source") == "sample"
    by_id: dict[str, dict] = {}
    for g in prev_games:
        if was_sample and not str(g.get("id", "")).startswith("psal-"):
            continue                                  # drop sample data on first real sync
        by_id[g["id"]] = copy.deepcopy(g)

    summary = {"added": 0, "updated": 0, "rescheduled": 0, "markedCanceled": 0, "kept": 0, "reidentified": 0}
    fresh_ids = {g["id"] for g in fresh.values()}
    for key, g in fresh.items():
        gid = g["id"]
        from_sport_page = bool(g.get("psal", {}).pop("_sportPage", False))
        old = by_id.get(gid)
        if old is None:
            # Same game under a different id (e.g. first seen on a team schedule, now on the calendar)?
            twin = next((o for oid, o in by_id.items() if oid not in fresh_ids and oid.startswith("psal-")
                         and same_game(o, g)), None)
            if twin is not None:
                del by_id[twin["id"]]
                old = twin
                summary["reidentified"] += 1
        if old is None:
            by_id[gid] = g
            summary["added"] += 1
            continue
        new = copy.deepcopy(g)
        # Calendar-only info is a guess for home/away; keep the earlier sport-page answer.
        if not from_sport_page and old.get("homeAway"):
            new["homeAway"] = old["homeAway"]
        # Never erase a published result because a later page didn't show it (see also below).
        if old.get("resultText") and not new.get("resultText") and new["status"] == "final":
            new["resultText"] = old["resultText"]
        if not new.get("time") and old.get("time"):
            new["time"] = old["time"]
        # Rescheduled: same PSAL game id, new date.
        if old.get("date") != new["date"] and old.get("status") != "final":
            try:
                od = date.fromisoformat(old["date"])
                new["statusNote"] = f"Rescheduled from {od:%b} {od.day}"
                summary["rescheduled"] += 1
            except (ValueError, KeyError):
                pass
        elif (old.get("statusNote") or "").startswith("Rescheduled from") and not new.get("statusNote"):
            new["statusNote"] = old["statusNote"]
        if isinstance(old.get("psal"), dict):
            for k in ("checkedAt", "checks", "teamCode"):
                if old["psal"].get(k) and not new.get("psal", {}).get(k):
                    new.setdefault("psal", {})[k] = old["psal"][k]
        # A 0-0 tie is a real score: test with "is not None", never truthiness.
        if old.get("score") is not None and new.get("score") is None and new["status"] not in ("canceled", "postponed"):
            new["score"], new["status"] = old["score"], "final"
        new.pop("missingRuns", None)
        if _comparable([old]) != _comparable([new]):
            summary["updated"] += 1
        by_id[gid] = new

    # Games we knew about that PSAL no longer lists.
    for gid, g in by_id.items():
        if gid in fresh_ids or not gid.startswith("psal-"):
            continue
        try:
            d = date.fromisoformat(g["date"])
        except (ValueError, KeyError):
            continue
        if not (window[0] <= d <= window[1]):
            continue                                  # outside what we checked: keep untouched
        if g.get("score") is not None or g.get("resultText"):
            summary["kept"] += 1                      # published results are history: keep
            continue
        tcode = (g.get("psal") or {}).get("teamCode") or (g.get("psal") or {}).get("sportCode")
        if tcode in known_codes and tcode not in verified_codes:
            if report is not None:                    # couldn't confirm this run: leave it alone
                report["unverifiedMissing"].append({"id": gid, "date": g["date"], "opponent": g.get("opponent")})
            continue
        g["missingRuns"] = int(g.get("missingRuns", 0)) + 1
        if g["missingRuns"] >= MISSING_RUNS_BEFORE_CANCEL and g.get("status") != "canceled":
            g["status"] = "canceled"
            g["statusNote"] = "Removed from PSAL schedule" if d >= today else "No longer listed by PSAL"
            summary["markedCanceled"] += 1

    games = sorted(by_id.values(), key=lambda x: (x.get("date", ""), x.get("time") or "99:99", x.get("sport", "")))
    return games, summary


def validate(fresh: dict, previous: dict, window: tuple[date, date], today: date) -> None:
    prev_psal_in_month = [
        g for g in previous.get("games", [])
        if str(g.get("id", "")).startswith("psal-") and g.get("date", "")[:7] == today.isoformat()[:7]
        and g.get("status") != "canceled"
    ]
    fresh_in_month = [g for g in fresh.values() if g["date"][:7] == today.isoformat()[:7]]
    if not fresh_in_month and len(prev_psal_in_month) >= 3:
        raise SyncError(
            f"PSAL calendar showed 0 games this month but we previously had {len(prev_psal_in_month)}. "
            "Treating this as a layout change and keeping the old data.")
    for g in fresh.values():
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", g["date"]) or not g["sport"] or g["gender"] not in ("Boys", "Girls", "Coed"):
            raise SyncError(f"Invalid game parsed: {g}")


def pick_detail_targets(fresh: dict[str, dict], previous: dict, now: datetime,
                        limit: int | None = None) -> list[tuple]:
    """
    Every game that has already started and has no published score is a candidate — no age limit.
    Order: games from the last 2 days, then never-checked games (newest first), then re-checks.
    Re-check spacing grows with age so old games don't use up the budget every run.
    """
    limit = limit or MAX_DETAIL_PAGES
    today = now.date()
    now_hm = now.strftime("%H:%M")
    prev = {g.get("id"): g for g in previous.get("games", [])}
    recent, never, again = [], [], []
    for key, g in fresh.items():
        if g["status"] in ("canceled", "postponed") or g.get("score") is not None or g.get("resultText"):
            continue
        if not g.get("opponent"):
            continue                          # meets/championships have no head-to-head score
        if not str((g.get("psal") or {}).get("gameId") or "").isdigit():
            continue                          # no game page to check (team-schedule entry without a link)
        old = prev.get(g["id"]) or {}
        if old.get("score") is not None or old.get("resultText"):
            continue                          # already have the final result
        d = date.fromisoformat(g["date"])
        if d > today or (d == today and (g.get("time") or "23:59") > now_hm):
            continue                          # not started yet — no score expected
        info = old.get("psal") or {}
        last, checks = info.get("checkedAt"), int(info.get("checks", 0) or 0)
        age = (today - d).days
        if age > GIVE_UP_AFTER_DAYS and checks >= GIVE_UP_AFTER_CHECKS:
            continue                          # PSAL never posted it; stop spending requests
        gap = (timedelta(0) if age <= 1 else timedelta(hours=1) if age <= 3
               else timedelta(hours=6) if age <= 14 else timedelta(hours=24))
        if last:
            try:
                if now - datetime.fromisoformat(last) < gap:
                    continue
            except ValueError:
                pass
        item = ((g["date"], g.get("time") or ""), (g["psal"]["sportCode"], g["psal"]["gameId"], g.get("opponent"), g.get("sport")))
        (recent if age <= 2 else never if not last else again).append(item)
    ordered = []
    for bucket in (recent, never, again):
        ordered += [t for _, t in sorted(bucket, key=lambda x: x[0], reverse=True)]
    return ordered[:limit]


def pick_sport_codes(fresh: dict[str, dict], previous: dict, now: datetime) -> list[str]:
    today = now.date()
    lo, hi = today + timedelta(days=SPORT_PAGE_NEAR_DAYS[0]), today + timedelta(days=SPORT_PAGE_NEAR_DAYS[1])
    last_full = (previous.get("meta", {}).get("sync") or {}).get("lastFullRefresh")
    full = True
    if last_full:
        try:
            full = now - datetime.fromisoformat(last_full) >= timedelta(hours=FULL_REFRESH_HOURS)
        except ValueError:
            full = True
    codes = set()
    for g in fresh.values():
        d = date.fromisoformat(g["date"])
        if full or lo <= d <= hi:
            codes.add(g["psal"]["sportCode"])
    return sorted(codes), full


# ---------------------------------------------------------------------------
# Files
# ---------------------------------------------------------------------------
def load_json(path: Path) -> dict | None:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("games"), list) or not isinstance(data.get("meta"), dict):
        return None
    return data


def best_previous(paths: Iterable[Path]) -> dict:
    """Pick the newest valid games.json (repo copy or the one currently live on Pages)."""
    best, best_ts = None, ""
    for p in paths:
        data = load_json(p) if p else None
        if data is None:
            continue
        ts = data["meta"].get("lastSynced") or ""
        try:
            ts_key = datetime.fromisoformat(ts).astimezone(TZ).isoformat() if ts else ""
        except ValueError:
            ts_key = ""
        if best is None or ts_key > best_ts:
            best, best_ts = data, ts_key
    if best is None:
        best = {"meta": {"schemaVersion": 1, "school": "Bronx High School of Science", "source": "none",
                         "lastSynced": None}, "games": []}
    return best


def write_json(path: Path, data: dict) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    tmp.replace(path)  # atomic: never leaves a half-written file


def gh_output(**kv) -> None:
    out = os.environ.get("GITHUB_OUTPUT")
    if out:
        with open(out, "a", encoding="utf-8") as f:
            for k, v in kv.items():
                f.write(f"{k}={v}\n")


def gh_summary(md: str) -> None:
    p = os.environ.get("GITHUB_STEP_SUMMARY")
    if p:
        with open(p, "a", encoding="utf-8") as f:
            f.write(md + "\n")


def build_output(previous: dict, games: list[dict], now: datetime, sync_info: dict) -> dict:
    meta = copy.deepcopy(previous.get("meta", {}))
    if meta.get("source") in ("sample", "none", None):
        meta.pop("dayNotes", None)                    # sample notes aren't real
    meta.update({
        "schemaVersion": 1,
        "school": "Bronx High School of Science",
        "source": "psal",
        "sourceUrl": CALENDAR_URL,
        "lastSynced": now.isoformat(timespec="seconds"),
        "notes": "Collected automatically from public pages on psal.org. Unofficial — PSAL's website is the official source.",
        "sync": sync_info,
    })
    meta.setdefault("seasons", [])
    meta.setdefault("dayNotes", {})
    return {"meta": meta, "games": games}


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------
REPORT_DIR = SCRIPT_DIR / "report"


def _hours_since(iso: str | None, now: datetime) -> float:
    if not iso:
        return 1e9
    try:
        return (now - datetime.fromisoformat(iso)).total_seconds() / 3600
    except ValueError:
        return 1e9


def discover_teams(fetcher: Fetcher, previous: dict, cal_teams: dict, now: datetime,
                   report: dict, warnings: list[str]) -> tuple[dict, list[dict], set]:
    """Find every Bronx Science team code (daily) from the team directory on each sport page."""
    prev_sync = previous.get("meta", {}).get("sync") or {}
    teams = copy.deepcopy(prev_sync.get("teams") or {})
    for t in teams.values():
        t.pop("readNow", None)
    due = not teams or _hours_since(prev_sync.get("lastDiscovery"), now) >= DISCOVERY_REFRESH_HOURS
    events: list[dict] = []
    fetched: set[str] = set()
    if due:
        codes: list[str] = []
        home = f"{BASE}/"
        if fetcher.allowed(home):
            try:
                codes = parse_sport_nav(fetcher.get_html(home, "home"))
            except AccessDenied:
                raise
            except SyncError as e:
                warnings.append(f"PSAL home page: {e}")
        if not codes:
            warnings.append("Couldn't list PSAL sport pages; team codes come from the school calendar only this run.")
        for code in codes:
            url = f"{BASE}{SPORT_PATH}?spCode={code}&flag=All"
            if not fetcher.allowed(url):
                report.setdefault("robotsSkipped", []).append(url)
                continue
            try:
                html = fetcher.get_html(url, f"sport_{code}")
            except AccessDenied:
                raise
            except SyncError as e:
                warnings.append(f"Sport page {code}: {e}")
                continue
            fetched.add(code)
            for tcode, info in parse_team_directory(html).items():
                t = teams.setdefault(tcode, {})
                t.update({k: v for k, v in info.items() if v})
            events += list(parse_sport_page(html).values())
    for code, info in cal_teams.items():
        t = teams.setdefault(code, {})
        for k in ("sport", "gender", "level"):
            if info.get(k) and not t.get(k):
                t[k] = info[k]
    if due and fetched:
        report["discoveryRan"] = True
    return teams, events, fetched


def pick_team_codes(teams: dict, fresh: dict, previous: dict, now: datetime) -> list[str]:
    today = now.date()
    near = set()
    recent = set()
    for g in list(fresh.values()) + previous.get("games", []):
        tc = (g.get("psal") or {}).get("teamCode") or (g.get("psal") or {}).get("sportCode")
        try:
            dd = (date.fromisoformat(g["date"]) - today).days
        except (KeyError, ValueError):
            continue
        if -45 <= dd <= 45:
            near.add(tc)
        if -1 <= dd <= 0:
            recent.add(tc)
    due = []
    for code, t in teams.items():
        in_season = code in near or t.get("gamesInWindow", 0) > 0
        gap = TEAM_REFRESH_HOURS if in_season else OFFSEASON_REFRESH_HOURS
        if code in recent or _hours_since(t.get("readAt"), now) >= gap:
            due.append((0 if code in recent else 1 if in_season else 2, t.get("readAt") or "", code))
    due.sort()
    return [c for _, _, c in due[:MAX_TEAM_PAGES]]


def run_sync(args, fetcher: Fetcher | None = None, now: datetime | None = None) -> int:
    now = now or datetime.now(TZ)
    today = now.date()
    data_path = Path(args.data)
    previous = best_previous([data_path] + [Path(p) for p in (args.previous or [])])
    out_path = (PREVIEW_DIR / "games.json") if args.preview else data_path
    if not args.preview:
        write_json(data_path, previous)               # the best known data is always in place
    fetcher = fetcher or Fetcher()
    warnings: list[str] = []
    report = new_report()
    try:
        fetcher.check_robots()
        fetcher.require_allowed(CALENDAR_URL)

        # 1. School calendar (primary list)
        pages, w = fetch_calendar(fetcher, today)
        warnings += w
        window = (month_bounds(*min(pages))[0], month_bounds(*max(pages))[1])
        cal_teams = {}
        for _ym, (_h, entries) in pages.items():
            for e in entries:
                t = parse_team_line(e.get("teamLine"))
                if t:
                    cal_teams.setdefault(e["code"], t)

        # 2. Every Bronx Science team (daily discovery from sport-page team directories)
        teams, discovery_events, fetched_sport = discover_teams(fetcher, previous, cal_teams, now, report, warnings)
        discovered = bool(fetched_sport)
        fresh, cal_entries, unclassified, w = build_fresh(pages, teams)
        warnings += w
        report["unclassified"] = unclassified
        validate(fresh, previous, window, today)

        # 3. Cross-check: each team's FULL SCHEDULE
        team_codes = pick_team_codes(teams, fresh, previous, now)
        schedules, w = fetch_team_schedules(fetcher, team_codes, DEBUG_DIR)
        warnings += w
        verified: set[str] = set()
        team_added = 0
        stamp = now.isoformat(timespec="seconds")
        for code in team_codes:
            rows = schedules.get(code)
            t = teams.setdefault(code, {})
            if rows is None:
                report["teamSchedulesUnreadable"].append(code)
                continue
            entries, _skipped = parse_team_rows(rows, code, t, today)
            entries = [e for e in entries if window[0].isoformat() <= e["date"] <= window[1].isoformat()]
            known_here = [g for g in fresh.values() if (g.get("psal") or {}).get("teamCode") == code]
            t.update({"readAt": stamp, "rows": len(rows), "games": len(entries), "gamesInWindow": len(entries), "readNow": True})
            if known_here and not entries:
                # The calendar shows games for this team but its schedule gave none: a reading problem, not proof of removal.
                report["teamSchedulesUnreadable"].append(code)
                continue
            verified.add(code)
            team_added += combine(fresh, entries, "team", report)

        # 4. Sport pages (today's games league-wide): times, home/away, status words
        sport_events = list(discovery_events)
        codes, full = pick_sport_codes(fresh, previous, now)
        for code in codes:
            url = f"{BASE}{SPORT_PATH}?spCode={code}&flag=All"
            if code in fetched_sport or not fetcher.allowed(url):
                continue                      # already read during team discovery this run
            try:
                sport_events += list(parse_sport_page(fetcher.get_html(url, f"sport_{code}")).values())
            except AccessDenied:
                raise
            except SyncError as e:
                warnings.append(f"Sport page {code}: {e}")
        sport_events = [e for e in sport_events if window[0].isoformat() <= e["date"] <= window[1].isoformat()]
        sport_added = combine(fresh, sport_events, "sportpage", report)

        # 5. Game pages: final scores
        targets = [] if args.no_details else pick_detail_targets(fresh, previous, now)
        details, w, _ = fetch_details(fetcher, targets, DEBUG_DIR)
        warnings += w
        scores_found = 0
        prev_by_id = {g.get("id"): g for g in previous.get("games", [])}
        detail_report = []
        by_key = {f"{g['psal'].get('sportCode')}/{g['psal'].get('gameId')}": g for g in fresh.values()}
        for key, d in details.items():
            g = by_key.get(key)
            if not g:
                continue
            if d.get("score") is not None and g.get("score") is not None and d["score"] != g["score"]:
                report["scoreConflicts"].append({"id": g["id"], "detail": d["score"], "team": g["score"]})
            apply_detail(g, d)
            if d.get("score") is not None:
                g["psal"]["scoreSource"] = "detail"
                scores_found += 1
            prev_checks = int(((prev_by_id.get(g["id"]) or {}).get("psal") or {}).get("checks", 0) or 0)
            g["psal"]["checkedAt"] = stamp
            g["psal"]["checks"] = prev_checks + 1
            detail_report.append((g, d))

        # 6. Merge with history (removals only when verified)
        known = set(teams) | {(g.get("psal") or {}).get("teamCode") for g in fresh.values()}
        games, summary = merge(previous, fresh, window, today, verified_codes=verified, known_codes=known, report=report)

        # 7. Completeness check
        for code, t in teams.items():
            report["teams"][code] = {k: t.get(k) for k in ("sport", "gender", "level", "rows", "games", "readAt", "readNow")}
        completeness(report, cal_entries, fresh, games, details, prev_by_id, now)
        write_reports(report, now)
        log_completeness(report, cal_entries)

        prev_sync = previous.get("meta", {}).get("sync") or {}
        for t in teams.values():
            t.pop("readNow", None)
        sync_info = {
            "status": "ok",
            "window": [window[0].isoformat(), window[1].isoformat()],
            "gamesInWindow": sum(1 for g in games if window[0].isoformat() <= g["date"] <= window[1].isoformat()),
            "calendarGames": len(cal_entries),
            "addedFromTeamSchedules": team_added,
            "addedFromSportPages": sport_added,
            "duplicatesMerged": len(report["duplicates"]),
            "calendarGamesNotCollected": len(report["missingFromCollected"]),
            "teamsKnown": len(teams),
            "teamSchedulesRead": len(verified),
            "teamSchedulesUnreadable": len(report["teamSchedulesUnreadable"]),
            "detailPagesChecked": len(details),
            "scoresFound": scores_found,
            "playedGamesStillWithoutScore": len(report["playedWithoutScore"]),
            "requests": fetcher.requests_made,
            "lastFullRefresh": stamp if full else prev_sync.get("lastFullRefresh"),
            "lastDiscovery": stamp if discovered else prev_sync.get("lastDiscovery"),
            "teams": teams,
            "changes": summary,
            "warnings": warnings[:25],
        }
        result = build_output(previous, games, now, sync_info)
        changed = _comparable(previous.get("games", [])) != _comparable(games) or previous["meta"].get("source") != "psal"
        write_json(out_path, result)
        log.info("Sync OK: %d games in window (%d from the calendar, +%d team schedules, +%d sport pages), changes=%s",
                 sync_info["gamesInWindow"], len(cal_entries), team_added, sport_added, summary)
        for w in warnings:
            log.warning(w)
        gh_output(status="ok", changed=str(changed and not args.preview).lower())
        gh_summary(summary_markdown(result, args.preview, detail_report))
        gh_summary(report_markdown(report, now))
        return EXIT_OK
    except SourceUnavailable as e:
        log.warning("PSAL temporarily unavailable — keeping previous data. %s", e)
        gh_output(status="unavailable", changed="false")
        gh_summary(f"⚠️ PSAL unavailable: {e}. Previous data kept.")
        return EXIT_OK
    except SyncError as e:
        log.error("Sync failed — keeping previous data. %s", e)
        gh_output(status="error", changed="false")
        gh_summary(f"❌ Sync failed: {e}. Previous data kept. Debug pages are attached to this run.")
        return EXIT_ERROR
    except Exception as e:  # unexpected bug: never destroy the old data
        log.exception("Unexpected error — keeping previous data.")
        gh_output(status="error", changed="false")
        gh_summary(f"❌ Unexpected error: {e!r}. Previous data kept.")
        return EXIT_ERROR


def write_reports(report: dict, now: datetime) -> None:
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    (REPORT_DIR / "completeness.json").write_text(json.dumps(report, indent=2, default=str), encoding="utf-8")
    (REPORT_DIR / "completeness.md").write_text(report_markdown(report, now), encoding="utf-8")


def _annotate(kind: str, title: str, msg: str) -> None:
    """GitHub Actions log annotation (shows on the run page); plain log line elsewhere."""
    if os.environ.get("GITHUB_ACTIONS"):
        print(f"::{kind} title={title}::{msg}", flush=True)
    else:
        log.warning("%s: %s", title, msg)


def log_completeness(report: dict, cal_entries: dict) -> None:
    log.info("Completeness: %d calendar games · %d not collected · %d found only on other PSAL pages · "
             "%d duplicates merged · %d played games without a score",
             len(cal_entries), len(report["missingFromCollected"]), len(report["notOnCalendar"]),
             len(report["duplicates"]), len(report["playedWithoutScore"]))
    for m in report["missingFromCollected"]:
        _annotate("warning", "Calendar game not collected", f"{m['date']} {m['teamLine']} {m['calendar']} (#{m['key']})")
    for code in report["teamSchedulesUnreadable"]:
        _annotate("warning", "Team schedule unreadable", f"{code}: {team_profile_url(code)}")
    for c in report["scoreConflicts"]:
        _annotate("warning", "Score conflict", f"{c['id']}: game page {c['detail']} vs team schedule {c['team']}")
    if report["notOnCalendar"]:
        _annotate("notice", "Games beyond the school calendar",
                  f"{len(report['notOnCalendar'])} Bronx Science games were found on team schedules / sport pages but not on the school calendar")
    for d, v in report["perDate"].items():
        log.info("  %s: calendar %d, collected %d, missing %s, only elsewhere %s",
                 d, v["calendar"], v["collected"], v["missing"] or "-", v["extra"] or "-")


def summary_markdown(data: dict, preview: bool, detail_report: list | None = None) -> str:
    s = data["meta"]["sync"]
    lines = [f"## {'Preview' if preview else 'Sync'} result",
             f"- Window: {s['window'][0]} → {s['window'][1]} · games: **{s['gamesInWindow']}** "
             f"({s['calendarGames']} on the school calendar, +{s['addedFromTeamSchedules']} from team schedules, "
             f"+{s['addedFromSportPages']} from sport pages, {s['duplicatesMerged']} duplicates merged)",
             f"- Teams: {s['teamsKnown']} known, {s['teamSchedulesRead']} schedules read, "
             f"{s['teamSchedulesUnreadable']} unreadable · "
             f"detail pages: {s['detailPagesChecked']} (scores read: {s['scoresFound']}) · "
             f"played games still without a score: {s.get('playedGamesStillWithoutScore', 'n/a')}",
             f"- Changes: `{json.dumps(s['changes'])}` · requests to PSAL: {s['requests']}"]
    if s["warnings"]:
        lines.append("- Warnings:\n" + "\n".join(f"  - {w}" for w in s["warnings"]))
    if detail_report:
        lines.append("\n### Game pages checked this run\n| Date | Team | Opponent | PSAL status | Score | Why |\n|---|---|---|---|---|---|")
        for g, d in sorted(detail_report, key=lambda x: x[0]["date"], reverse=True):
            sc = d.get("score")
            lines.append(f"| {g['date']} | {g['gender']} {g['level']} {g['sport']} | {g.get('opponent') or ''} | "
                         f"{d.get('status') or '—'} | "
                         f"{(str(sc['bxsci']) + '-' + str(sc['opponent'])) if sc is not None else '—'} | "
                         f"{d.get('scoreReason', '')} |")
    lines.append("\n| Date | Time | Team | Opponent / event | H/A | Location | Status | Score |\n|---|---|---|---|---|---|---|---|")
    lo, hi = s["window"]
    for g in data["games"]:
        if lo <= g["date"] <= hi:
            sc = g.get("score")
            score_txt = f"{sc['bxsci']}-{sc['opponent']}" if sc is not None else (g.get("resultText") or "")
            lines.append(f"| {g['date']} | {g.get('time') or 'TBA'} | {g['gender']} {g['level']} {g['sport']} | "
                         f"{g.get('opponent') or g.get('event') or ''} | {g['homeAway']} | {g.get('location', '')} | "
                         f"{g['status']} | {score_txt} |")
    return "\n".join(lines)


def run_probe(args) -> int:
    """Save raw pages so the parsers can be checked against the real site."""
    fetcher = Fetcher(debug_dir=DEBUG_DIR)
    report: list[str] = ["# PSAL probe"]
    try:
        fetcher.check_robots()
        robots = fetcher._request("GET", f"{BASE}/robots.txt", check_robots=False)
        (DEBUG_DIR / "robots.txt").write_text(robots.text if robots.status_code == 200 else f"HTTP {robots.status_code}")
        report.append(f"- robots.txt: HTTP {robots.status_code}; calendar allowed: {fetcher.allowed(CALENDAR_URL)}; "
                      f"sport pages allowed: {fetcher.allowed(BASE + SPORT_PATH + '?spCode=016')}; "
                      f"game pages allowed: {fetcher.allowed(BASE + DETAIL_PATH)}")
        html = fetcher.get_html(CALENDAR_URL, "calendar_current")
        ym, entries, w = parse_calendar(html)
        report.append(f"- Calendar month: {ym}; game links parsed: {len(entries)}; warnings: {w[:5]}")
        soup = BeautifulSoup(html, "html.parser")
        report.append(f"- Next-month control: {find_nav(soup, 'next', CALENDAR_URL)}")
        for e in entries[:8]:
            report.append(f"  - {e['date']} | {e['teamLine']} | {e['text']} | #{e['code']}/{e['gid']}")
        if entries:
            code = entries[0]["code"]
            sp = fetcher.get_html(f"{BASE}{SPORT_PATH}?spCode={code}&flag=All", f"sport_{code}")
            info = parse_sport_page(sp)
            report.append(f"- Sport page {code}: Bronx Science events parsed: {len(info)}; "
                          f"Bronx Science teams in its directory: {parse_team_directory(sp)}")
            home_codes = parse_sport_nav(fetcher.get_html(f"{BASE}/", "home")) if fetcher.allowed(f"{BASE}/") else []
            report.append(f"- Sport pages linked from PSAL navigation: {len(home_codes)} {home_codes[:40]}")
            tcodes = list(dict.fromkeys(e["code"] for e in entries))[:2]
            sched, w3 = fetch_team_schedules(fetcher, tcodes, DEBUG_DIR)
            for tc in tcodes:
                rows = sched.get(tc)
                t = parse_team_line(next((e["teamLine"] for e in entries if e["code"] == tc), "")) or {}
                parsed, skipped = parse_team_rows(rows or [], tc, t, datetime.now(TZ).date())
                report.append(f"- Team schedule {tc} ({team_profile_url(tc)}): rows {None if rows is None else len(rows)}, "
                              f"games parsed {len(parsed)}, skipped {skipped} {w3[:2]}")
                for g in parsed[:6]:
                    report.append(f"  - {g['date']} {g.get('time')} vs {g.get('opponent')} ({g.get('homeAway')}) "
                                  f"result={g.get('result')} round={g.get('round')} id={g.get('gid')} | {g['raw'][:90]}")
            for k, v in list(info.items())[:5]:
                report.append(f"  - {k}: {v}")
            today = datetime.now(TZ).date().isoformat()
            past = [e for e in entries if e["date"] < today][-4:] or entries[:1]
            targets = [(e["code"], e["gid"], parse_link_text(e["text"])["opponent"],
                        (parse_team_line(e.get("teamLine")) or {}).get("sport")) for e in past]
            res, w2, net = fetch_details(fetcher, targets, DEBUG_DIR, capture_network=True)
            (DEBUG_DIR / "detail_network.txt").write_text("\n".join(net), encoding="utf-8")
            report.append(f"- Detail pages rendered: {len(res)}; warnings: {w2}")
            for k, v in res.items():
                report.append(f"  - {k}: status={v.get('status')} time={v.get('time')} score={v.get('score')} "
                              f"({v.get('scoreReason')})")
            report.append(f"- Network requests made by the detail page (look for a JSON endpoint): {len(net)} saved to detail_network.txt")
    except (SyncError, SourceUnavailable) as e:
        report.append(f"- Stopped: {e}")
    text = "\n".join(report)
    DEBUG_DIR.mkdir(parents=True, exist_ok=True)
    (DEBUG_DIR / "probe-report.md").write_text(text, encoding="utf-8")
    print(text)
    gh_summary(text)
    return EXIT_OK


def run_prepare(args) -> int:
    """Put the newest valid games.json in place (used before deploying site-only changes)."""
    data = best_previous([Path(args.data)] + [Path(p) for p in (args.previous or [])])
    write_json(Path(args.data), data)
    log.info("Prepared %s (source=%s, lastSynced=%s, %d games)", args.data, data["meta"].get("source"),
             data["meta"].get("lastSynced"), len(data["games"]))
    return EXIT_OK


CSV_COLUMNS = ["date", "time", "sport", "gender", "level", "opponent", "event", "homeAway", "location",
               "status", "bxsci_score", "opponent_score", "resultText", "statusNote", "round", "id"]


def run_import_csv(args) -> int:
    """Manual / authorized-feed fallback: a CSV (e.g. Google Sheet export) becomes games.json."""
    rows = list(csv.DictReader(open(args.csv, newline="", encoding="utf-8-sig")))
    games, errors = [], []
    for i, r in enumerate(rows, start=2):
        r = {k.strip(): (v or "").strip() for k, v in r.items() if k}
        try:
            d = date.fromisoformat(r["date"])
        except (KeyError, ValueError):
            errors.append(f"row {i}: bad date {r.get('date')!r} (use YYYY-MM-DD)")
            continue
        t = r.get("time", "")
        t = t if re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", t) else parse_time(t)
        a, b = r.get("bxsci_score", ""), r.get("opponent_score", "")
        score = {"bxsci": int(a), "opponent": int(b)} if a.isdigit() and b.isdigit() else None
        status = (r.get("status") or ("final" if score else "scheduled")).lower()
        games.append({
            "id": r.get("id") or f"manual-{d.isoformat()}-{i}",
            "date": d.isoformat(), "time": t,
            "sport": r.get("sport") or "Other", "gender": r.get("gender") or "Coed",
            "level": r.get("level") or "Varsity", "opponent": r.get("opponent") or None,
            "event": r.get("event") or None, "round": r.get("round") or None,
            "homeAway": (r.get("homeAway") or "neutral").lower(), "location": r.get("location") or "Location TBA",
            "status": status, "score": score, "resultText": r.get("resultText") or None,
            "statusNote": r.get("statusNote") or None,
        })
    if errors:
        for e in errors:
            log.error(e)
        return EXIT_ERROR
    previous = load_json(Path(args.data)) or {"meta": {}}
    meta = copy.deepcopy(previous["meta"])
    if meta.get("source") == "sample":
        meta.pop("dayNotes", None)
    meta.update({"schemaVersion": 1, "school": "Bronx High School of Science", "source": "manual",
                 "lastSynced": datetime.now(TZ).isoformat(timespec="seconds"),
                 "notes": "Entered manually from official schedules."})
    meta.pop("sync", None)
    meta.setdefault("seasons", [])
    meta.setdefault("dayNotes", {})
    games.sort(key=lambda g: (g["date"], g["time"] or "99:99"))
    write_json(Path(args.data), {"meta": meta, "games": games})
    log.info("Imported %d games from %s", len(games), args.csv)
    return EXIT_OK


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("sync", "preview"):
        sp = sub.add_parser(name)
        sp.add_argument("--data", default="games.json")
        sp.add_argument("--previous", action="append", help="other copies of games.json (e.g. the live one)")
        sp.add_argument("--no-details", action="store_true", help="skip rendering game-detail pages")
    sub.add_parser("probe")
    pp = sub.add_parser("prepare")
    pp.add_argument("--data", default="games.json")
    pp.add_argument("--previous", action="append")
    ic = sub.add_parser("import-csv")
    ic.add_argument("csv")
    ic.add_argument("--data", default="games.json")
    args = ap.parse_args(argv)
    if args.cmd in ("sync", "preview"):
        args.preview = args.cmd == "preview"
        return run_sync(args)
    if args.cmd == "probe":
        return run_probe(args)
    if args.cmd == "prepare":
        return run_prepare(args)
    return run_import_csv(args)


if __name__ == "__main__":
    sys.exit(main())
