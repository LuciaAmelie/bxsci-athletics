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
MONTHS_BACK = 1            # calendar months before the current one
MONTHS_AHEAD = 2           # calendar months after the current one
DETAIL_LOOKBACK_DAYS = 14  # look for scores on games up to this many days old
MAX_DETAIL_PAGES = int(os.environ.get("SYNC_MAX_DETAIL_PAGES", "20"))
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
BXS_RE = re.compile(r"bronx\s+(?:h\.?\s*s\.?|high\s+school)\s+(?:of\s+)?science|bronx\s+science", re.I)
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


def calendar_entry_to_game(e: dict) -> dict | None:
    team = parse_team_line(e.get("teamLine"))
    if not team:
        return None
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
        "round": team.get("round"),
        "homeAway": home_away,
        "location": link["location"] or "Location TBA",
        "status": "scheduled",
        "score": None,
        "resultText": None,
        "statusNote": None,
        "psal": {"sportCode": e["code"], "gameId": e["gid"], "url": detail_url(e["code"], e["gid"])},
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
        info: dict[str, Any] = {
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
                 "head coaches", "game/meet results", "athletic trainer"]


VENUE_WORDS_RE = re.compile(
    r"\b(gym|field|pool|park|court|courts|lanes|stadium|track|oval|campus\s*-|st\.?|street|ave|avenue|"
    r"blvd|road|rd|room|natatorium|map)\b", re.I)


def _name_matches(a: str, b: str | None) -> bool:
    """Loose team-name match: share a distinctive word of 4+ letters."""
    if not b:
        return False
    stop = {"high", "school", "campus", "educational", "academy", "the", "and", "for", "of"}
    wa = {w for w in re.findall(r"[a-z]{4,}", a.lower()) if w not in stop}
    wb = {w for w in re.findall(r"[a-z]{4,}", b.lower()) if w not in stop}
    return bool(wa & wb)


def parse_detail_text(text: str, opponent: str | None = None) -> dict:
    """Read status/time and, only when unambiguous, the final score from a rendered detail page."""
    lines = [norm(l) for l in text.splitlines()]
    lines = [l for l in lines if l]
    low = [l.lower().rstrip(":") for l in lines]
    label_value_idx: set[int] = set()

    def value(label: str) -> str | None:
        for i, l in enumerate(low):
            if l == label:
                for j in range(i + 1, min(i + 3, len(lines))):
                    if low[j] not in DETAIL_LABELS:
                        label_value_idx.add(j)
                        return lines[j]
                return None
            if l.startswith(label + ":"):
                label_value_idx.add(i)
                return lines[i].split(":", 1)[1].strip() or None
            # Table rows render as "Label<TAB>Value" -> "Label Value" on one line.
            if l.startswith(label + " "):
                rest = lines[i][len(label):].strip()
                if rest.lower() not in DETAIL_LABELS and not any(
                        l.startswith(other) for other in DETAIL_LABELS if len(other) > len(label)):
                    label_value_idx.add(i)
                    return rest or None
        return None

    out: dict[str, Any] = {"status": value("status"), "time": parse_time(value("time") or ""), "score": None}
    for lab in ("sport", "game/meet", "date", "address", "referees", "head coaches", "athletic trainer"):
        value(lab)

    # Score: exactly one "<Bronx Science name> <n>" line and one "<opponent name> <m>" line.
    pairs = []
    for i, l in enumerate(lines):
        if i in label_value_idx or low[i] in DETAIL_LABELS or VENUE_WORDS_RE.search(l):
            continue
        if len(re.findall(r"\d+", l)) != 1:
            continue
        m = re.match(r"^(.*[A-Za-z].*?)\s*[|:\-–]?\s*(\d{1,3})$", l)
        if m:
            pairs.append((m.group(1).strip(), int(m.group(2))))
    bx = [p for p in pairs if BXS_RE.search(p[0])]
    other = [p for p in pairs if not BXS_RE.search(p[0]) and (opponent is None or _name_matches(p[0], opponent))]
    if len(bx) == 1 and len(other) == 1:
        out["score"] = {"bxsci": bx[0][1], "opponent": other[0][1]}
    out["resultLines"] = [f"{n} {s}" for n, s in pairs][:10]
    return out


def fetch_details(fetcher: Fetcher, targets: list[tuple[str, str, str | None]], debug_dir: Path | None,
                  capture_network: bool = False) -> tuple[dict, list[str], list[str]]:
    """Render game-detail pages. Returns (results, warnings, network_urls)."""
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
                    if r.resource_type in ("xhr", "fetch", "script") else None)
        for code, gid, opponent in targets:
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
                text = page.inner_text("body")
            except Exception as e:  # one bad page shouldn't sink the whole run
                warnings.append(f"Could not render {url}: {e}")
                continue
            if debug_dir:
                debug_dir.mkdir(parents=True, exist_ok=True)
                (debug_dir / f"detail_{code}_{gid}.txt").write_text(text, encoding="utf-8")
            results[game_key(code, gid)] = parse_detail_text(text, opponent)
        browser.close()
    return results, warnings, network


def apply_detail(g: dict, d: dict) -> None:
    status_text = (d.get("status") or "").lower()
    if d.get("time") and not g.get("time"):
        g["time"] = d["time"]
    if "cancel" in status_text:
        g["status"], g["score"] = "canceled", None
    elif "postpon" in status_text:
        g["status"], g["score"] = "postponed", None
    elif "forfeit" in status_text:
        g["status"], g["resultText"] = "final", g.get("resultText") or "Forfeit"
    if d.get("score") and g["status"] not in ("canceled", "postponed"):
        g["score"] = d["score"]
        g["status"] = "final"


# ---------------------------------------------------------------------------
# Building, merging and validating
# ---------------------------------------------------------------------------
def build_fresh(pages: dict) -> tuple[dict[str, dict], list[str]]:
    fresh: dict[str, dict] = {}
    warnings: list[str] = []
    missing_team = 0
    total = 0
    for _ym, (_html, entries) in sorted(pages.items()):
        for e in entries:
            total += 1
            g = calendar_entry_to_game(e)
            if g is None:
                missing_team += 1
                continue
            fresh[game_key(e["code"], e["gid"])] = g
    if total and missing_team / total > 0.5:
        raise SyncError(f"{missing_team} of {total} calendar games had no sport/team line — layout may have changed.")
    if missing_team:
        warnings.append(f"Skipped {missing_team} calendar entries without a sport/team line.")
    return fresh, warnings


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
            g2["psal"].pop("checkedAt", None)
        clean.append(g2)
    return json.dumps(sorted(clean, key=lambda x: x.get("id", "")), sort_keys=True)


def merge(previous: dict, fresh: dict[str, dict], window: tuple[date, date], today: date) -> tuple[list[dict], dict]:
    prev_meta = previous.get("meta", {})
    prev_games = previous.get("games", [])
    was_sample = prev_meta.get("source") == "sample"
    by_id: dict[str, dict] = {}
    for g in prev_games:
        if was_sample and not str(g.get("id", "")).startswith("psal-"):
            continue                                  # drop sample data on first real sync
        by_id[g["id"]] = copy.deepcopy(g)

    summary = {"added": 0, "updated": 0, "rescheduled": 0, "markedCanceled": 0, "kept": 0}
    fresh_ids = set()
    for key, g in fresh.items():
        gid = g["id"]
        fresh_ids.add(gid)
        from_sport_page = bool(g.get("psal", {}).pop("_sportPage", False))
        old = by_id.get(gid)
        if old is None:
            by_id[gid] = g
            summary["added"] += 1
            continue
        new = copy.deepcopy(g)
        # Calendar-only info is a guess for home/away; keep the earlier sport-page answer.
        if not from_sport_page and old.get("homeAway"):
            new["homeAway"] = old["homeAway"]
        # Never erase a published result because a later page didn't show it.
        if old.get("score") and not new.get("score") and new["status"] not in ("canceled", "postponed"):
            new["score"], new["status"] = old["score"], "final"
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
        if isinstance(old.get("psal"), dict) and old["psal"].get("checkedAt"):
            new.setdefault("psal", {})["checkedAt"] = old["psal"]["checkedAt"]
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
        if d < today:
            summary["kept"] += 1                      # history: keep past games and results
            continue
        g["missingRuns"] = int(g.get("missingRuns", 0)) + 1
        if g["missingRuns"] >= MISSING_RUNS_BEFORE_CANCEL and g.get("status") != "canceled":
            g["status"] = "canceled"
            g["statusNote"] = "Removed from PSAL schedule"
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


def pick_detail_targets(fresh: dict[str, dict], previous: dict, now: datetime) -> list[tuple[str, str]]:
    today = now.date()
    now_hm = now.strftime("%H:%M")
    prev_checked = {g["id"]: (g.get("psal") or {}).get("checkedAt") for g in previous.get("games", [])}
    prev_scored = {g["id"] for g in previous.get("games", []) if g.get("score") or g.get("resultText")}
    cands = []
    for key, g in fresh.items():
        if g["status"] in ("canceled", "postponed") or g["id"] in prev_scored or g.get("score"):
            continue
        d = date.fromisoformat(g["date"])
        started = d < today or (d == today and (g.get("time") or "23:59") <= now_hm)
        if not started or d < today - timedelta(days=DETAIL_LOOKBACK_DAYS):
            continue
        age = (today - d).days
        gap = timedelta(0) if age <= 1 else timedelta(hours=3) if age <= 3 else timedelta(hours=12)
        last = prev_checked.get(g["id"])
        if last:
            try:
                if now - datetime.fromisoformat(last) < gap:
                    continue
            except ValueError:
                pass
        cands.append((g["date"], g.get("time") or "", key, g.get("opponent")))
    cands.sort(key=lambda c: (c[0], c[1]), reverse=True)  # most recent first
    return [(*k.split("/"), opp) for _, _, k, opp in cands[:MAX_DETAIL_PAGES]]


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
    try:
        fetcher.check_robots()
        fetcher.require_allowed(CALENDAR_URL)
        pages, w = fetch_calendar(fetcher, today)
        warnings += w
        first = min(pages)
        last = max(pages)
        window = (month_bounds(*first)[0], month_bounds(*last)[1])
        fresh, w = build_fresh(pages)
        warnings += w
        validate(fresh, previous, window, today)

        codes, full = pick_sport_codes(fresh, previous, now)
        matched = 0
        for code in codes:
            url = f"{BASE}{SPORT_PATH}?spCode={code}&flag=All"
            if not fetcher.allowed(url):
                warnings.append(f"robots.txt disallows sport page {code}; times/home-away for it use calendar info only.")
                continue
            matched += apply_sport_info(fresh, parse_sport_page(fetcher.get_html(url, f"sport_{code}")))

        targets = [] if args.no_details else pick_detail_targets(fresh, previous, now)
        details, w, _ = fetch_details(fetcher, targets, DEBUG_DIR)
        warnings += w
        scores_found = 0
        checked_at = now.isoformat(timespec="seconds")
        for key, d in details.items():
            g = fresh.get(key)
            if g:
                apply_detail(g, d)
                g["psal"]["checkedAt"] = checked_at
                scores_found += 1 if d.get("score") else 0

        games, summary = merge(previous, fresh, window, today)
        prev_sync = previous.get("meta", {}).get("sync") or {}
        sync_info = {
            "status": "ok",
            "window": [window[0].isoformat(), window[1].isoformat()],
            "gamesInWindow": len(fresh),
            "sportPages": len(codes),
            "sportPageMatches": matched,
            "detailPagesChecked": len(details),
            "scoresFound": scores_found,
            "requests": fetcher.requests_made,
            "lastFullRefresh": now.isoformat(timespec="seconds") if full else prev_sync.get("lastFullRefresh"),
            "changes": summary,
            "warnings": warnings[:20],
        }
        result = build_output(previous, games, now, sync_info)
        changed = _comparable(previous.get("games", [])) != _comparable(games) or previous["meta"].get("source") != "psal"
        write_json(out_path, result)
        log.info("Sync OK: %d games in window, %d total, changes=%s", len(fresh), len(games), summary)
        for w in warnings:
            log.warning(w)
        gh_output(status="ok", changed=str(changed and not args.preview).lower())
        gh_summary(summary_markdown(result, args.preview))
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


def summary_markdown(data: dict, preview: bool) -> str:
    s = data["meta"]["sync"]
    lines = [f"## {'Preview' if preview else 'Sync'} result",
             f"- Window: {s['window'][0]} → {s['window'][1]} · games found: **{s['gamesInWindow']}**",
             f"- Sport pages: {s['sportPages']} (matched {s['sportPageMatches']} games) · "
             f"detail pages: {s['detailPagesChecked']} (scores read: {s['scoresFound']})",
             f"- Changes: `{json.dumps(s['changes'])}` · requests to PSAL: {s['requests']}"]
    if s["warnings"]:
        lines.append("- Warnings:\n" + "\n".join(f"  - {w}" for w in s["warnings"]))
    lines.append("\n| Date | Time | Team | Opponent / event | H/A | Location | Status | Score |\n|---|---|---|---|---|---|---|---|")
    lo, hi = s["window"]
    for g in data["games"]:
        if lo <= g["date"] <= hi:
            sc = g.get("score")
            score_txt = f"{sc['bxsci']}-{sc['opponent']}" if sc else (g.get("resultText") or "")
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
            report.append(f"- Sport page {code}: Bronx Science events parsed: {len(info)}")
            for k, v in list(info.items())[:5]:
                report.append(f"  - {k}: {v}")
            today = datetime.now(TZ).date().isoformat()
            past = [e for e in entries if e["date"] < today][-2:] or entries[:1]
            targets = [(e["code"], e["gid"], parse_link_text(e["text"])["opponent"]) for e in past]
            res, w2, net = fetch_details(fetcher, targets, DEBUG_DIR, capture_network=True)
            (DEBUG_DIR / "detail_network.txt").write_text("\n".join(net), encoding="utf-8")
            report.append(f"- Detail pages rendered: {len(res)}; warnings: {w2}")
            for k, v in res.items():
                report.append(f"  - {k}: status={v.get('status')} time={v.get('time')} score={v.get('score')} "
                              f"result lines={v.get('resultLines')}")
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
