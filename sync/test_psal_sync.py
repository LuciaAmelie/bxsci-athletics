"""
Tests for psal_sync.py — run with:  python -m unittest sync/test_psal_sync.py -v

IMPORTANT: the HTML fixtures below are SYNTHETIC. They reproduce the text
patterns visible on psal.org (school calendar, sport pages, game-detail
labels) but not necessarily PSAL's exact HTML tags. They prove the parsing,
merging and safety logic; they do not prove the live site matches. Use
`python sync/psal_sync.py probe` (or the "probe" workflow run) for that.
"""
import calendar
import json
import os
import sys
import tempfile
import threading
import unittest
from datetime import date, datetime
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
import psal_sync as ps  # noqa: E402

TZ = ps.TZ


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------
def calendar_html(year, month, events, prev_arg="V_PREV", next_arg="V_NEXT", padding=True, viewstate="vs"):
    """ASP.NET-style month grid. events: {date: [(team_line, link_text, code, gid)]}"""
    cal = calendar.Calendar(firstweekday=6)  # Sunday first, like PSAL
    rows = []
    for week in cal.monthdatescalendar(year, month):
        cells = []
        for d in week:
            if d.month != month and not padding:
                cells.append("<td></td>")
                continue
            parts = [f'<a href="javascript:__doPostBack(\'ctl00$cal\',\'{d.toordinal()}\')">{d.day}</a>']
            for team, text, code, gid in events.get(d, []):
                parts.append(f"<br/>{team}<br/><a href=\"https://www.psal.org/games/game-detail.aspx#{code}/{gid}\">{text}</a>")
            cells.append(f"<td>{''.join(parts)}</td>")
        rows.append("<tr>" + "".join(cells) + "</tr>")
    return f"""<html><body><form method="post" action="./school-calendar.aspx?schid=10501" id="aspnetForm">
<input type="hidden" name="__VIEWSTATE" value="{viewstate}"/>
<input type="hidden" name="__EVENTVALIDATION" value="ev"/>
<h2>School Sports Calendar</h2>
School : <select name="ctl00$ddlSchool"><option value="">--Select--</option><option value="10501" selected="selected">The Bronx High School of Science</option></select>
<a href="mailto:;">email</a> <a href="#">print</a>
<table class="cal"><tr>
<td><a href="javascript:__doPostBack('ctl00$cal','{prev_arg}')" title="Go to the previous month">&lt;&lt;</a></td>
<td>{calendar.month_name[month]} {year}</td>
<td><a href="javascript:__doPostBack('ctl00$cal','{next_arg}')" title="Go to the next month">&gt;&gt;</a></td></tr>
<tr><th>Sunday</th><th>Monday</th><th>Tuesday</th><th>Wednesday</th><th>Thursday</th><th>Friday</th><th>Saturday</th></tr>
{''.join(rows)}</table></form><div>©2021 PSAL</div></body></html>"""


def directory(sport_heading, rows):
    """Team directory table like PSAL's: rows = (school, team code, school id, type)."""
    trs = "".join(f'<tr><td><a href="https://www.psal.org/profiles/team-profile.aspx#{c}/{sid}">{school}</a></td>'
                  f'<td><a href="https://www.psal.org/profiles/team-profile.aspx#{c}/{sid}">wolverines</a></td>'
                  f'<td>{typ}</td><td>3A -- Bronx</td><td>Bronx</td></tr>' for school, c, sid, typ in rows)
    return (f"<h2>{sport_heading}</h2><table><tr><th>SCHOOL</th><th>TEAM</th><th>TYPE</th><th>DIVISION</th>"
            f"<th>BOROUGH</th></tr>{trs}</table>")


def sport_page(items, extra=""):
    """items: (code, gid, team, mmddyyyy, time, body, venue) like PSAL's list."""
    lis = []
    for code, gid, team, d, t, body, venue in items:
        lis.append(f"""<li>Bronx &nbsp;&nbsp; <a href="https://www.psal.org/games/game-detail.aspx#{code}/{gid}">Details &gt;&gt;</a>
<br/>{team}<br/>{d} {t}<br/>{body} @ {venue} <a href="#">map</a> 297</li>""")
    return f"<html><body><h1>PSAL</h1><ul>{''.join(lis)}</ul>{extra}</body></html>"


NOV = {
    date(2026, 11, 2): [("SOCCER GIRLS VARSITY", "Vs. Stuyvesant High School @ Harris Park: Soccer-01", "012", "511700"),
                        ("BADMINTON BOYS VARSITY", "Vs. Seward Park Campus @ Bronx HS Science - Gym 2", "067", "511625")],
    date(2026, 11, 7): [("CROSS COUNTRY BOYS", "City Championships @ Van Cortlandt Park - Track & Field", "033", "9871")],
    date(2026, 11, 9): [("VOLLEYBALL GIRLS VARSITY", "Vs. East Harlem Pride @ Bronx HS Of Science - GYM", "016", "511884")],
    date(2026, 11, 10): [("TENNIS GIRLS VARSITY", "Vs. Townsend Harris High School @ Queens College Tennis #1", "014", "511990")],
    date(2026, 11, 12): [("VOLLEYBALL GIRLS JR. VARSITY", "Vs. Bronx United @ Bronx HS Of Science - GYM", "038", "512001")],
    date(2026, 11, 23): [("SWIMMING BOYS VARSITY", "Vs. Evander Childs Campus @ Evander Childs Pool", "013", "512100")],
    date(2026, 11, 30): [("BASKETBALL BOYS JR. VARSITY", "Vs. Roosevelt Educational Campus @ Bronx HS Of Science - GYM", "002", "512200")],
    date(2026, 12, 2): [("SWIMMING BOYS VARSITY", "Vs. Harry S. Truman @ Walton HS - Pool", "013", "512101")],
}
OCT = {date(2026, 10, 20): [("TENNIS GIRLS VARSITY", "Vs. Stuyvesant High School @ Williamsbridge Oval Tennis Courts", "014", "511001")],
       date(2026, 9, 30): [("SOCCER BOYS VARSITY", "Vs. Lehman Campus @ Macombs Dam Park: Soccer-01", "011", "510001")]}
DEC = {date(2026, 12, 2): NOV[date(2026, 12, 2)],
       date(2026, 12, 9): [("TABLE TENNIS BOYS VARSITY", "Vs. Adlai Stevenson Campus @ Bronx HS of Science - Table Tennis", "065", "513000")]}
SEP = {date(2026, 9, 30): OCT[date(2026, 9, 30)],
       date(2026, 9, 15): [("SOCCER GIRLS VARSITY", "Vs. Lehman Campus @ Macombs Dam Park: Soccer-01", "012", "509001")]}
JAN = {date(2027, 1, 6): [("GYMNASTICS GIRLS VARSITY", "Vs. DeWitt Clinton High School @ Dewitt Clinton-Gym", "009", "514000")]}

HOME_PAGE = ("<html><body><nav>" + "".join(
    f'<a href="https://www.psal.org/sports/sport.aspx?spCode={c}&flag=All">x</a>' for c in ("016", "012", "014", "033", "026"))
    + '<a href="https://www.psal.org/sports/sport.aspx?spCode=016&flag=MPL">mpl</a></nav></body></html>')

SPORT_PAGES = {
    "016": sport_page([("016", "511884", "VOLLEYBALL GIRLS V", "11/09/2026", "4:30 PM",
                        "The Bronx High School of Science vs East Harlem Pride", "Bronx HS Of Science - GYM"),
                       ("016", "599999", "VOLLEYBALL GIRLS V", "11/09/2026", "4:30 PM",
                        "John Jay Campus vs Tottenville High School", "John Jay Campus - GYM")],
                      directory("VOLLEYBALL 016", [("Bronx Science", "038", "10501", "girls JV"),
                                                   ("Bronx Science", "016", "10501", "girls v"),
                                                   ("A-Tech HS", "016", "14547", "girls v")])),
    "014": sport_page([("014", "511990", "TENNIS GIRLS V", "11/10/2026", "5:00 pm",
                        "Townsend Harris High School vs The Bronx High School of Science", "Queens College Tennis #1")],
                      directory("TENNIS 014", [("Bronx Science", "014", "10501", "girls v")])),
    "038": sport_page([("038", "512001", "VOLLEYBALL GIRLS JV", "11/12/2026", "04:00 PM",
                        "postponed early The Bronx High School of Science vs Bronx United", "Bronx HS Of Science - GYM")]),
    "012": sport_page([("012", "511700", "SOCCER GIRLS V", "11/02/2026", "2:30 PM",
                        "The Bronx High School of Science vs Stuyvesant High School", "Harris Park: Soccer-01")],
                      directory("SOCCER 012", [("Bronx Science", "012", "10501", "girls v"),
                                               ("Bronx Science", "011", "10501", "boys v")])),
    "033": sport_page([], directory("CROSS COUNTRY 033", [("Bronx Science", "033", "10501", "boys"),
                                                          ("Bronx Science", "077", "10501", "girls")])),
}

def row(cells, link=None):
    return {"cells": cells, "links": [f"https://www.psal.org/games/game-detail.aspx#{link}"] if link else []}


TEAM_ROWS = {
    # girls JV volleyball: one game missing from the school calendar (with a published result)
    "038": [row(["11/12/2026", "vs. Bronx United", "4:00 PM", "Postponed"], "038/512001"),
            row(["11/05/2026", "@ Lehman Campus", "4:00 PM", "W 2-0"], "038/512050"),
            row(["11/11/2026", "Practice", "3:30 PM"])],
    # girls varsity volleyball: calendar game + a playoff game that isn't on the calendar
    "016": [row(["11/09/2026", "vs. East Harlem Pride", "4:30 PM", "W 3-1"], "016/511884"),
            row(["11/16/2026", "PSAL Playoffs - Round of 32", "vs. Midwood High School", "4:00 PM"], "016/520001")],
    # girls soccer: the calendar game + a second game the same day (doubleheader, different id and time)
    "012": [row(["11/02/2026", "vs. Stuyvesant", "2:30 PM"], "012/511700"),
            row(["11/02/2026", "vs. Stuyvesant", "5:00 PM"], "012/511701")],
    # girls tennis: the calendar game listed under another id (duplicate) + another school's sidebar game
    "014": [row(["11/10/2026", "@ Townsend Harris HS", "5:00 PM", "L 2-3"], "055/888888"),
            row(["11/10/2026", "Hunter vs Beacon", "4:00 PM"], "014/999999")],
    # girls cross country: a whole team that's missing from the school calendar
    "077": [row(["11/14/2026", "Bronx Borough Championships @ Van Cortlandt Park", "9:30 AM"], "077/9900")],
    # boys cross country: page could not be read
    "033": None,
}


def fake_team_schedules(fetcher, codes, debug_dir):
    return {c: TEAM_ROWS.get(c, []) for c in codes}, []


DETAIL_TEXTS = {
    "016/511884": """GAME/MEET RESULTS
Sport
VOLLEYBALL GIRLS V
Game/Meet
The Bronx High School of Science vs East Harlem Pride
Status
Final
Date
11/09/2026
Time
4:30 PM
Address
Bronx HS Of Science - GYM 75 West 205 Street
The Bronx High School of Science 3
East Harlem Pride 1
Referees
John Doe
©2021 PSAL""",
    "012/511700": """Sport
SOCCER GIRLS V
Status
Final
Time
2:30 PM
Address
Harris Park: Soccer-01
Set 1 25 20
The Bronx High School of Science 25 25 25
Stuyvesant 20 18 22""",   # ambiguous on purpose -> must NOT produce a score
}


class FakeResponse:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class FakeFetcher(ps.Fetcher):
    """Serves fixtures instead of the network. Records requests."""
    def __init__(self, robots="User-agent: *\nDisallow: /admin/\n", fail=None):
        super().__init__(delay=0, debug_dir=None)
        self.robots_txt, self.fail, self.log = robots, fail, []
        self.months = {(2026, 9): SEP, (2026, 10): OCT, (2026, 11): NOV, (2026, 12): DEC, (2027, 1): JAN}
        self.args = {(2026, 9): ("V_P9", "V_N9"), (2026, 10): ("V_P10", "V_N10"), (2026, 11): ("V_P11", "V_N11"),
                     (2026, 12): ("V_P12", "V_N12"), (2027, 1): ("V_P1", "V_N1")}

    def _page(self, ym):
        p, n = self.args[ym]
        return calendar_html(ym[0], ym[1], self.months[ym], p, n)

    def _request(self, method, url, check_robots=True, **kw):
        if check_robots:
            self.require_allowed(url)
        self.log.append((method, url, (kw.get("data") or {}).get("__EVENTARGUMENT")))
        self.requests_made += 1
        if self.fail == "down":
            raise ps.SourceUnavailable("simulated outage")
        if url.rstrip("/") == "https://www.psal.org":
            return FakeResponse(200, HOME_PAGE)
        if url.endswith("/robots.txt"):
            return FakeResponse(200 if self.robots_txt is not None else 404, self.robots_txt or "")
        if "school-calendar" in url:
            if self.fail == "layout":
                return FakeResponse(200, "<html><body>Under maintenance</body></html>")
            if method == "GET":
                return FakeResponse(200, self._page((2026, 11)))
            arg = kw["data"]["__EVENTARGUMENT"]
            for ym, (p, n) in self.args.items():
                if arg == p:
                    return FakeResponse(200, self._page(ps.add_months(*ym, -1)))
                if arg == n:
                    return FakeResponse(200, self._page(ps.add_months(*ym, 1)))
            return FakeResponse(200, self._page((2026, 11)))
        if "sport.aspx" in url:
            code = url.split("spCode=")[1].split("&")[0]
            return FakeResponse(200, SPORT_PAGES.get(code, sport_page([])))
        return FakeResponse(404, "")


def fake_details(fetcher, targets, debug_dir, capture_network=False):
    res = {}
    for code, gid, opp, sport in targets:
        t = DETAIL_TEXTS.get(f"{code}/{gid}")
        if t:
            res[f"{code}/{gid}"] = ps.parse_detail_text(t, opp, sport)
    return res, [], []


def sample_previous():
    return json.loads((Path(__file__).resolve().parent.parent / "games.json").read_text())


# ---------------------------------------------------------------------------
# Unit tests
# ---------------------------------------------------------------------------
class ParserTests(unittest.TestCase):
    def test_team_line(self):
        self.assertEqual(ps.parse_team_line("VOLLEYBALL GIRLS JR. VARSITY"),
                         {"sport": "Volleyball", "gender": "Girls", "level": "JV"})
        self.assertEqual(ps.parse_team_line("CROSS COUNTRY BOYS F/S"),
                         {"sport": "Cross Country", "gender": "Boys", "level": "JV", "round": "Frosh/Soph"})
        self.assertEqual(ps.parse_team_line("TABLE TENNIS BOYS VARSITY")["sport"], "Table Tennis")
        self.assertEqual(ps.parse_team_line("CROSS COUNTRY BOYS")["level"], "Varsity")
        self.assertIsNone(ps.parse_team_line("Vs. Something"))

    def test_link_text(self):
        self.assertEqual(ps.parse_link_text("Vs. Townsend Harris High School @ Queens College Tennis #1"),
                         {"opponent": "Townsend Harris", "event": None, "location": "Queens College Tennis #1"})
        r = ps.parse_link_text("City Championships @ Van Cortlandt Park - Track & Field")
        self.assertEqual((r["opponent"], r["event"]), (None, "City Championships"))
        self.assertIsNone(ps.parse_link_text("Vs. New Utrecht High School @")["location"])

    def test_time(self):
        self.assertEqual(ps.parse_time("4:30 PM"), "16:30")
        self.assertEqual(ps.parse_time("04:00 pm"), "16:00")
        self.assertEqual(ps.parse_time("12:15 PM"), "12:15")
        self.assertEqual(ps.parse_time("12:05 AM"), "00:05")
        self.assertEqual(ps.parse_time("9:30 AM"), "09:30")
        self.assertIsNone(ps.parse_time("TBA"))

    def test_calendar_dates_including_padding_days(self):
        ym, entries, warns = ps.parse_calendar(calendar_html(2026, 11, NOV))
        self.assertEqual(ym, (2026, 11))
        by_gid = {e["gid"]: e for e in entries}
        self.assertEqual(by_gid["511625"]["date"], "2026-11-02")
        self.assertEqual(by_gid["511625"]["teamLine"], "BADMINTON BOYS VARSITY")
        self.assertEqual(by_gid["511700"]["teamLine"], "SOCCER GIRLS VARSITY")
        self.assertEqual(by_gid["512101"]["date"], "2026-12-02")   # trailing padding day -> December
        ym, entries, _ = ps.parse_calendar(calendar_html(2026, 10, OCT))
        self.assertEqual({e["gid"]: e["date"] for e in entries}["510001"], "2026-09-30")  # leading padding -> September

    def test_calendar_entry_home_away(self):
        _, entries, _ = ps.parse_calendar(calendar_html(2026, 11, NOV))
        games = {e["gid"]: ps.calendar_entry_to_game(e) for e in entries}
        self.assertEqual(games["511625"]["homeAway"], "home")      # venue is Bronx HS Science
        self.assertEqual(games["511990"]["homeAway"], "away")
        self.assertEqual(games["9871"]["homeAway"], "neutral")     # meet
        self.assertEqual(games["9871"]["event"], "City Championships")
        self.assertEqual(games["512001"]["level"], "JV")

    def test_nav_and_postback_payload(self):
        soup = ps.BeautifulSoup(calendar_html(2026, 11, NOV, "P", "N"), "html.parser")
        self.assertEqual(ps.find_nav(soup, "next", ps.CALENDAR_URL), ("postback", "ctl00$cal", "N"))
        self.assertEqual(ps.find_nav(soup, "prev", ps.CALENDAR_URL), ("postback", "ctl00$cal", "P"))
        action, data = ps.form_payload(soup, "ctl00$cal", "N")
        self.assertTrue(action.startswith("https://www.psal.org/events-and-standings/school-calendar.aspx?schid=10501"))
        self.assertEqual(data["__VIEWSTATE"], "vs")
        self.assertEqual(data["ctl00$ddlSchool"], "10501")
        self.assertEqual(data["__EVENTARGUMENT"], "N")

    def test_sport_page(self):
        info = ps.parse_sport_page(SPORT_PAGES["016"])
        self.assertEqual(set(info), {"016/511884"})                   # other schools ignored
        self.assertEqual(info["016/511884"]["time"], "16:30")
        self.assertEqual(info["016/511884"]["homeAway"], "home")
        t = ps.parse_sport_page(SPORT_PAGES["014"])["014/511990"]
        self.assertEqual((t["homeAway"], t["opponent"], t["time"]), ("away", "Townsend Harris", "17:00"))
        p = ps.parse_sport_page(SPORT_PAGES["038"])["038/512001"]
        self.assertEqual(ps.status_from_words(p["statusWords"])[0], "postponed")
        self.assertEqual(p["opponent"], "Bronx United")

    def test_detail_score_only_when_unambiguous(self):
        d = ps.parse_detail_text(DETAIL_TEXTS["016/511884"], "East Harlem Pride")
        self.assertEqual(d["status"], "Final")
        self.assertEqual(d["time"], "16:30")
        self.assertEqual(d["score"], {"bxsci": 3, "opponent": 1})
        self.assertIsNone(ps.parse_detail_text(DETAIL_TEXTS["012/511700"], "Stuyvesant")["score"])
        # wrong opponent name -> refuse
        self.assertIsNone(ps.parse_detail_text(DETAIL_TEXTS["016/511884"], "Brooklyn Tech")["score"])
        # venue line "Bronx HS Of Science - GYM 2" must never be read as a score
        self.assertIsNone(ps.parse_detail_text("Status\nScheduled\nAddress\nBronx HS Of Science - GYM 2\n", "X")["score"])


class MergeTests(unittest.TestCase):
    def setUp(self):
        self.window = (date(2026, 10, 1), date(2027, 1, 31))
        self.today = date(2026, 11, 10)

    def g(self, gid, d, **kw):
        base = {"id": f"psal-012-{gid}", "date": d, "time": None, "sport": "Soccer", "gender": "Girls",
                "level": "Varsity", "opponent": "X", "event": None, "round": None, "homeAway": "home",
                "location": "Field", "status": "scheduled", "score": None, "resultText": None, "statusNote": None,
                "psal": {"sportCode": "012", "gameId": gid}}
        base.update(kw)
        return base

    def test_drops_sample_and_adds(self):
        prev = sample_previous()
        games, s = ps.merge(prev, {"012/1": self.g("1", "2026-11-12")}, self.window, self.today)
        self.assertEqual([x["id"] for x in games], ["psal-012-1"])
        self.assertEqual(s["added"], 1)

    def test_keeps_score_history_and_marks_removed_future_games(self):
        prev = {"meta": {"source": "psal"}, "games": [
            self.g("1", "2026-11-05", status="final", score={"bxsci": 2, "opponent": 1}),
            self.g("2", "2026-11-20"),
            self.g("3", "2025-11-01", status="final", score={"bxsci": 1, "opponent": 0}),   # old season
        ]}
        fresh = {"012/1": self.g("1", "2026-11-05")}            # PSAL page no longer shows the score
        games, s = ps.merge(prev, fresh, self.window, self.today)
        by = {x["id"]: x for x in games}
        self.assertEqual(by["psal-012-1"]["score"], {"bxsci": 2, "opponent": 1})
        self.assertEqual(by["psal-012-1"]["status"], "final")
        self.assertIn("psal-012-3", by)                          # history outside window kept
        self.assertEqual(by["psal-012-2"]["status"], "scheduled")  # first miss: not yet canceled
        games2, s2 = ps.merge({"meta": {"source": "psal"}, "games": games}, fresh, self.window, self.today)
        self.assertEqual({x["id"]: x for x in games2}["psal-012-2"]["status"], "canceled")
        self.assertEqual(s2["markedCanceled"], 1)

    def test_reschedule_note(self):
        prev = {"meta": {"source": "psal"}, "games": [self.g("1", "2026-11-12")]}
        games, s = ps.merge(prev, {"012/1": self.g("1", "2026-11-17")}, self.window, self.today)
        self.assertEqual(games[0]["statusNote"], "Rescheduled from Nov 12")
        self.assertEqual(s["rescheduled"], 1)

    def test_best_previous_prefers_newest(self):
        with tempfile.TemporaryDirectory() as d:
            a, b = Path(d, "a.json"), Path(d, "b.json")
            a.write_text(json.dumps({"meta": {"lastSynced": "2026-11-10T20:00:00-05:00"}, "games": [1]}))
            b.write_text(json.dumps({"meta": {"lastSynced": "2026-11-10T22:00:00-05:00"}, "games": [2]}))
            self.assertEqual(ps.best_previous([a, b])["games"], [2])
            Path(d, "c.json").write_text("{not json")
            self.assertEqual(ps.best_previous([Path(d, "c.json"), a])["games"], [1])


def page(status, *body, sport_line="SOCCER GIRLS V", game="The Bronx High School of Science vs Stuyvesant"):
    """A rendered game-detail page: labelled fields, then whatever results block PSAL shows."""
    return "\n".join(["GAME/MEET RESULTS", "Sport", sport_line, "Game/Meet", game, "Status", status,
                       "Date", "11/03/2026", "Time", "4:00 PM", "Address", "Harris Park: Soccer-01 map 2639",
                       *body, "Referees", "Pat Smith", "©2021 PSAL"])


class ScoreFormatTests(unittest.TestCase):
    """Different sports / layouts PSAL may use. Each must give Bronx Science first."""

    def sc(self, text, opp="Stuyvesant", sport="Soccer"):
        return ps.parse_detail_text(text, opp, sport)["score"]

    def test_A_name_and_score_rows(self):
        self.assertEqual(self.sc(page("Final", "The Bronx High School of Science 3", "Stuyvesant High School 1")),
                         {"bxsci": 3, "opponent": 1})

    def test_A_tab_separated_table_rows(self):
        self.assertEqual(self.sc(page("Final", "Stuyvesant High School\t2", "The Bronx High School of Science\t4")),
                         {"bxsci": 4, "opponent": 2})

    def test_B_name_and_score_on_separate_lines(self):
        self.assertEqual(self.sc(page("Final", "The Bronx High School of Science", "1", "Stuyvesant High School", "2")),
                         {"bxsci": 1, "opponent": 2})

    def test_C_one_line_formats(self):
        for line, want in [("Bronx Science 2 - 1 Stuyvesant", (2, 1)),
                           ("Stuyvesant 1 - 2 Bronx Science", (2, 1)),
                           ("Stuyvesant 3, Bronx HS of Science 0", (0, 3)),
                           ("The Bronx High School of Science vs Stuyvesant: 5-4", (5, 4))]:
            with self.subTest(line=line):
                self.assertEqual(self.sc(page("Final", line)), {"bxsci": want[0], "opponent": want[1]})

    def test_D_basketball_box_score_with_total(self):
        t = page("Final", "Team 1 2 3 4 Total", "The Bronx High School of Science 10 12 8 15 45",
                 "Lehman Campus 8 9 10 11 38", game="Lehman Campus vs The Bronx High School of Science")
        self.assertEqual(self.sc(t, "Lehman Campus", "Basketball"), {"bxsci": 45, "opponent": 38})

    def test_E_volleyball_sets_without_total(self):
        t = page("Final", "Team Set 1 Set 2 Set 3 Set 4", "The Bronx High School of Science 25 23 25 25",
                 "East Harlem Pride 20 25 18 21", sport_line="VOLLEYBALL GIRLS V")
        self.assertEqual(self.sc(t, "East Harlem Pride", "Volleyball"), {"bxsci": 3, "opponent": 1})

    def test_sets_are_not_summed_for_non_set_sports(self):
        t = page("Final", "Set 1 Set 2", "The Bronx High School of Science 25 25", "Stuyvesant 20 18")
        self.assertIsNone(self.sc(t, "Stuyvesant", "Soccer"))

    def test_gymnastics_decimal_scores(self):
        t = page("Final", "Team Score", "the bronx high school of science 110.00", "stuyvesant high school 98.35",
                 sport_line="GYMNASTICS GIRLS V")
        self.assertEqual(self.sc(t, "Stuyvesant", "Gymnastics"), {"bxsci": 110, "opponent": 98.35})

    def test_swimming_and_tennis_style_totals(self):
        self.assertEqual(self.sc(page("Final", "Bronx Science 54", "Hunter College HS 40"), "Hunter College", "Swimming"),
                         {"bxsci": 54, "opponent": 40})
        self.assertEqual(self.sc(page("Final", "Townsend Harris 1", "Bronx Science 4"), "Townsend Harris", "Tennis"),
                         {"bxsci": 4, "opponent": 1})

    def test_real_zero_zero_tie_when_final(self):
        self.assertEqual(self.sc(page("Final", "The Bronx High School of Science 0", "Stuyvesant 0")),
                         {"bxsci": 0, "opponent": 0})

    def test_zero_zero_placeholder_is_not_a_result(self):
        self.assertIsNone(self.sc(page("Scheduled", "The Bronx High School of Science 0", "Stuyvesant 0")))
        self.assertIsNone(self.sc(page("", "The Bronx High School of Science 0", "Stuyvesant 0")))   # unknown status

    def test_nonzero_score_with_missing_status_is_accepted(self):
        self.assertEqual(self.sc(page("", "The Bronx High School of Science 2", "Stuyvesant 0")),
                         {"bxsci": 2, "opponent": 0})

    def test_postponed_or_scheduled_pages_never_give_scores(self):
        for st in ("Postponed", "Cancelled", "Scheduled", "Not Played"):
            with self.subTest(status=st):
                self.assertIsNone(self.sc(page(st, "The Bronx High School of Science 3", "Stuyvesant 1")))

    def test_conflicting_layouts_give_no_score(self):
        t = page("Final", "The Bronx High School of Science 3", "Stuyvesant 1", "Bronx Science 2 - 1 Stuyvesant")
        d = ps.parse_detail_text(t, "Stuyvesant", "Soccer")
        self.assertIsNone(d["score"])
        self.assertIn("conflicting", d["scoreReason"])

    def test_abbreviated_opponent_names(self):
        t = page("Final", "Bronx Science 61", "Bklyn Tech. 58", "Referee crew 3")
        self.assertEqual(self.sc(t, "Brooklyn Technical", "Basketball"), {"bxsci": 61, "opponent": 58})

    def test_venue_date_time_lines_are_never_scores(self):
        t = page("Final", "Bronx HS Of Science - GYM 2", "11/03/2026 4:00 PM")
        d = ps.parse_detail_text(t, "Stuyvesant", "Soccer")
        self.assertIsNone(d["score"])
        self.assertEqual(d["scoreReason"], "no score lines for both teams were found on the page")

    def test_final_status_without_numbers_stays_unpublished(self):
        g = {"status": "scheduled", "score": None, "time": None, "resultText": None}
        ps.apply_detail(g, ps.parse_detail_text(page("Final"), "Stuyvesant", "Soccer"))
        self.assertEqual((g["status"], g["score"]), ("scheduled", None))   # site shows "Awaiting result"


class CoverageTests(unittest.TestCase):
    NOW = datetime(2026, 11, 11, 10, 0, tzinfo=TZ)

    def game(self, gid, d, t="16:00", **kw):
        g = {"id": f"psal-012-{gid}", "date": d, "time": t, "sport": "Soccer", "gender": "Girls", "level": "Varsity",
             "opponent": "Opp", "event": None, "homeAway": "home", "status": "scheduled", "score": None,
             "resultText": None, "psal": {"sportCode": "012", "gameId": str(gid)}}
        g.update(kw)
        return g

    def test_all_played_games_are_candidates_not_just_last_14_days(self):
        fresh = {}
        for i in range(70):   # Sept 2 .. Nov 10: 70 played games
            d = (date(2026, 11, 10).toordinal() - i)
            fresh[f"012/{i}"] = self.game(i, date.fromordinal(d).isoformat())
        fresh["012/future"] = self.game("future", "2026-11-20")
        fresh["012/today-later"] = self.game("today-later", "2026-11-11", "16:00")
        fresh["012/meet"] = self.game("meet", "2026-11-01", opponent=None, event="City Championships")
        t = ps.pick_detail_targets(fresh, {"games": []}, self.NOW, limit=1000)
        gids = [x[1] for x in t]
        self.assertEqual(len(gids), 70)
        self.assertNotIn("future", gids)
        self.assertNotIn("today-later", gids)
        self.assertNotIn("meet", gids)
        self.assertEqual(gids[0], "0")                       # newest first
        self.assertEqual(len(ps.pick_detail_targets(fresh, {"games": []}, self.NOW, limit=60)), 60)

    def test_games_with_results_are_skipped_including_ties(self):
        fresh = {"012/1": self.game(1, "2026-11-01")}
        prev = {"games": [self.game(1, "2026-11-01", status="final", score={"bxsci": 0, "opponent": 0})]}
        self.assertEqual(ps.pick_detail_targets(fresh, prev, self.NOW), [])

    def test_recheck_spacing_and_give_up(self):
        fresh = {"012/1": self.game(1, "2026-10-22"), "012/2": self.game(2, "2026-09-20")}
        prev = {"games": [
            self.game(1, "2026-10-22", psal={"checkedAt": "2026-11-11T08:00:00-05:00", "checks": 2}),
            self.game(2, "2026-09-20", psal={"checkedAt": "2026-11-09T08:00:00-05:00", "checks": 6})]}
        self.assertEqual(ps.pick_detail_targets(fresh, prev, self.NOW), [])   # checked 2h ago; given up
        prev["games"][0]["psal"]["checkedAt"] = "2026-11-10T08:00:00-05:00"     # 26h ago -> due again
        self.assertEqual([x[1] for x in ps.pick_detail_targets(fresh, prev, self.NOW)], ["1"])

    def test_zero_zero_result_is_never_erased_by_merge(self):
        prev = {"meta": {"source": "psal"}, "games": [
            self.game(1, "2026-11-01", status="final", score={"bxsci": 0, "opponent": 0})]}
        games, _ = ps.merge(prev, {"012/1": self.game(1, "2026-11-01")}, (date(2026, 9, 1), date(2027, 1, 31)),
                            date(2026, 11, 11))
        self.assertEqual((games[0]["status"], games[0]["score"]), ("final", {"bxsci": 0, "opponent": 0}))


class TeamDiscoveryTests(unittest.TestCase):
    def test_sport_nav_codes(self):
        self.assertEqual(ps.parse_sport_nav(HOME_PAGE), ["016", "012", "014", "033", "026"])   # MPL link ignored

    def test_directory_finds_only_bronx_science_teams(self):
        teams = ps.parse_team_directory(SPORT_PAGES["016"])
        self.assertEqual(set(teams), {"038", "016"})        # A-Tech's 016/14547 is ignored
        self.assertEqual((teams["038"]["sport"], teams["038"]["gender"], teams["038"]["level"]), ("Volleyball", "Girls", "JV"))
        self.assertEqual(teams["016"]["level"], "Varsity")


class TeamScheduleTests(unittest.TestCase):
    TODAY = date(2026, 11, 11)

    def parse(self, code, info=None):
        return ps.parse_team_rows(TEAM_ROWS[code], code, info or {"sport": "Volleyball", "gender": "Girls", "level": "JV"}, self.TODAY)

    def test_rows_become_games_and_practice_is_skipped(self):
        entries, skipped = self.parse("038")
        self.assertEqual([(e["gid"], e["date"], e["opponent"], e["homeAway"]) for e in entries],
                         [("512001", "2026-11-12", "Bronx United", "home"), ("512050", "2026-11-05", "Lehman Campus", "away")])
        self.assertEqual(skipped, 1)
        self.assertEqual(entries[1]["result"], {"bxsci": 2, "opponent": 0})
        self.assertEqual(entries[0]["statusWords"], "postponed")

    def test_other_schools_rows_are_ignored(self):
        entries, skipped = self.parse("014", {"sport": "Tennis", "gender": "Girls", "level": "Varsity"})
        self.assertEqual([e["gid"] for e in entries], ["888888"])
        self.assertEqual(skipped, 1)

    def test_meet_row_keeps_event_and_venue(self):
        (e,), _ = self.parse("077", {"sport": "Cross Country", "gender": "Girls", "level": "Varsity"})
        self.assertEqual((e["opponent"], e["event"], e["location"], e["round"]),
                         (None, "Bronx Borough Championships", "Van Cortlandt Park", "Championship"))

    def test_year_is_inferred_for_month_day_dates(self):
        self.assertEqual(ps.resolve_md_date(1, 6, None, date(2026, 11, 11)), date(2027, 1, 6))
        self.assertEqual(ps.resolve_md_date(9, 15, None, date(2026, 11, 11)), date(2026, 9, 15))
        self.assertEqual(ps.resolve_md_date(9, 15, 25, date(2026, 11, 11)), date(2025, 9, 15))


class ResultTextTests(unittest.TestCase):
    def test_orientation_by_letter(self):
        cases = {"W 3-1": (3, 1), "L 1-3": (1, 3), "L 3-1": (1, 3), "3-1 W": (3, 1), "1-3 (L)": (1, 3),
                 "T 1-1": (1, 1), "T 0-0": (0, 0), "W, 54-40": (54, 40), "W 110.5-98.35": (110.5, 98.35)}
        for text, (a, b) in cases.items():
            with self.subTest(text=text):
                self.assertEqual(ps.result_from_text(text), {"bxsci": a, "opponent": b})

    def test_no_letter_or_contradiction_means_no_result(self):
        for text in ("0-0", "3-1", "W 2-2", "T 3-1", "4:30 PM", "11/12/2026", "Round of 32"):
            with self.subTest(text=text):
                self.assertIsNone(ps.result_from_text(text))


class PlayoffTests(unittest.TestCase):
    def test_labels(self):
        self.assertEqual(ps.playoff_label("PSAL Playoffs - Round of 32"), "Playoffs · Round of 32")
        self.assertEqual(ps.playoff_label("Quarterfinal vs. Midwood"), "Playoffs · Quarterfinal")
        self.assertEqual(ps.playoff_label("City Championships"), "Championship")
        self.assertIsNone(ps.playoff_label("vs. Lehman Campus"))

    def test_playoff_game_on_calendar_gets_round(self):
        e = {"code": "016", "gid": "520001", "date": "2026-11-16", "teamLine": "VOLLEYBALL GIRLS VARSITY",
             "text": "Playoffs Round of 32 Vs. Midwood High School @ Bronx HS Of Science - GYM"}
        g = ps.calendar_entry_to_game(e)
        self.assertEqual((g["round"], g["homeAway"]), ("Playoffs · Round of 32", "home"))


class DuplicateTests(unittest.TestCase):
    def cal_game(self, gid, d, opp, t=None):
        g = ps.calendar_entry_to_game({"code": "012", "gid": gid, "date": d, "teamLine": "SOCCER GIRLS VARSITY",
                                       "text": f"Vs. {opp} @ Harris Park: Soccer-01"})
        g["time"] = t
        return g

    def entry(self, code, gid, d, opp, t=None, **kw):
        e = {"code": code, "gid": gid, "teamCode": "012", "date": d, "time": t, "opponent": opp, "homeAway": "home",
             "location": None, "result": None, "statusWords": None, "round": None, "sport": "Soccer",
             "gender": "Girls", "level": "Varsity"}
        e.update(kw)
        return e

    def test_same_game_under_another_id_is_merged(self):
        fresh = {"012/1": self.cal_game("1", "2026-11-02", "Stuyvesant High School")}
        rep = ps.new_report()
        added = ps.combine(fresh, [self.entry("099", "77", "2026-11-02", "Stuyvesant", "15:30")], "team", rep)
        self.assertEqual((added, len(fresh), len(rep["duplicates"])), (0, 1, 1))
        self.assertEqual(fresh["012/1"]["time"], "15:30")
        self.assertEqual(fresh["012/1"]["psal"]["aliases"], ["099/77"])

    def test_doubleheader_is_two_games(self):
        fresh = {"012/1": self.cal_game("1", "2026-11-02", "Stuyvesant", "14:30")}
        rep = ps.new_report()
        ps.combine(fresh, [self.entry("012", "2", "2026-11-02", "Stuyvesant", "17:00")], "team", rep)
        self.assertEqual(len(fresh), 2)
        self.assertEqual(rep["duplicates"], [])

    def test_same_source_twice_and_padding_days_dont_duplicate(self):
        _, entries, _ = ps.parse_calendar(calendar_html(2026, 11, NOV))
        pages = {(2026, 11): ("", entries), (2026, 12): ("", [e for e in entries if e["date"].startswith("2026-12")])}
        fresh, cal, _, _ = ps.build_fresh(pages)
        self.assertEqual(len(fresh), len(cal))
        self.assertEqual(sum(1 for g in fresh.values() if g["id"] == "psal-013-512101"), 1)

    def test_other_team_same_day_is_not_a_duplicate(self):
        fresh = {"012/1": self.cal_game("1", "2026-11-02", "Stuyvesant")}
        ps.combine(fresh, [self.entry("011", "5", "2026-11-02", "Stuyvesant", gender="Boys")], "team", ps.new_report())
        self.assertEqual(len(fresh), 2)


class MissingGameTests(unittest.TestCase):
    def test_calendar_game_without_team_line_is_kept_and_classified(self):
        e = {"code": "038", "gid": "1", "date": "2026-11-12", "teamLine": None, "text": "Vs. Bronx United @"}
        fresh, cal, unclassified, warns = ps.build_fresh({(2026, 11): ("", [e])},
                                                         {"038": {"sport": "Volleyball", "gender": "Girls", "level": "JV"}})
        g = fresh["038/1"]
        self.assertEqual((g["sport"], g["gender"], g["level"], g["location"]), ("Volleyball", "Girls", "JV", "Location TBA"))
        self.assertEqual(len(unclassified), 1)
        fresh, *_ = ps.build_fresh({(2026, 11): ("", [e])}, {})
        self.assertEqual(fresh["038/1"]["sport"], "Other")        # still kept with no team info at all

    def test_games_without_time_opponent_or_location_are_kept(self):
        g = ps.new_game_from_entry({"code": "077", "gid": None, "teamCode": "077", "date": "2026-11-14",
                                    "sport": "Cross Country", "gender": "Girls", "level": "Varsity"}, "team")
        self.assertEqual((g["time"], g["opponent"], g["location"], g["status"], g["score"]),
                         (None, None, "Location TBA", "scheduled", None))
        self.assertTrue(g["id"].startswith("psal-077-s20261114"))

    def test_completeness_reports_a_calendar_game_that_was_not_collected(self):
        cal = {"016/1": {"code": "016", "gid": "1", "date": "2026-11-09", "teamLine": "VOLLEYBALL GIRLS VARSITY", "text": "Vs. X @ Y"},
               "016/2": {"code": "016", "gid": "2", "date": "2026-11-09", "teamLine": "VOLLEYBALL GIRLS VARSITY", "text": "Vs. Z @ Y"}}
        games = [ps.calendar_entry_to_game(cal["016/1"])]
        rep = ps.completeness(ps.new_report(), cal, {"016/1": games[0]}, games, {}, {}, datetime(2026, 11, 11, 12, tzinfo=TZ))
        self.assertEqual([m["key"] for m in rep["missingFromCollected"]], ["016/2"])
        self.assertEqual(rep["perDate"]["2026-11-09"]["calendar"], 2)
        self.assertEqual(rep["perDate"]["2026-11-09"]["collected"], 1)

    def test_missing_score_is_explained_not_zero(self):
        g = ps.calendar_entry_to_game({"code": "016", "gid": "1", "date": "2026-11-09", "teamLine": "VOLLEYBALL GIRLS VARSITY",
                                       "text": "Vs. X @ Y"})
        rep = ps.completeness(ps.new_report(), {}, {"016/1": g}, [g], {}, {}, datetime(2026, 11, 11, 12, tzinfo=TZ))
        self.assertIsNone(g["score"])
        self.assertIn("not checked yet", rep["playedWithoutScore"][0]["reason"])


class RemovalVerificationTests(unittest.TestCase):
    WINDOW = (date(2026, 9, 1), date(2027, 1, 31))
    TODAY = date(2026, 11, 11)

    def prev_game(self, gid, d, code, **kw):
        g = {"id": f"psal-{code}-{gid}", "date": d, "time": None, "sport": "Volleyball", "gender": "Girls",
             "level": "Varsity", "opponent": "Opp", "event": None, "homeAway": "home", "location": "Gym",
             "status": "scheduled", "score": None, "resultText": None, "statusNote": None,
             "psal": {"sportCode": code, "gameId": gid, "teamCode": code}}
        g.update(kw)
        return g

    def run_merge(self, prev_games, verified):
        prev = {"meta": {"source": "psal"}, "games": prev_games}
        rep = ps.new_report()
        games, s = ps.merge(prev, {}, self.WINDOW, self.TODAY, verified_codes=verified, known_codes={"016", "033"}, report=rep)
        return {g["id"]: g for g in games}, s, rep

    def test_past_unscored_game_removed_only_after_two_verified_runs(self):
        g = self.prev_game("1", "2026-11-03", "016")
        by, _, _ = self.run_merge([g], {"016"})
        self.assertEqual(by["psal-016-1"]["status"], "scheduled")
        by, s, _ = self.run_merge(list(by.values()), {"016"})
        self.assertEqual((by["psal-016-1"]["status"], by["psal-016-1"]["statusNote"]), ("canceled", "No longer listed by PSAL"))

    def test_unverified_absence_never_removes(self):
        g = self.prev_game("9", "2026-11-03", "033")
        for _ in range(3):
            by, _, rep = self.run_merge([g], set())      # team 033 schedule unreadable
            g = by["psal-033-9"]
        self.assertEqual(g["status"], "scheduled")
        self.assertEqual(rep["unverifiedMissing"][0]["id"], "psal-033-9")

    def test_published_results_are_kept(self):
        g = self.prev_game("2", "2026-11-03", "016", status="final", score={"bxsci": 0, "opponent": 0})
        by, _, _ = self.run_merge([g], {"016"})
        by, _, _ = self.run_merge(list(by.values()), {"016"})
        self.assertEqual((by["psal-016-2"]["status"], by["psal-016-2"]["score"]), ("final", {"bxsci": 0, "opponent": 0}))

    def test_game_first_seen_on_team_schedule_keeps_its_score_when_calendar_id_appears(self):
        old = self.prev_game("s20261105-lehman", "2026-11-05", "038", opponent="Lehman Campus", status="final",
                             score={"bxsci": 2, "opponent": 0}, level="JV")
        new = ps.calendar_entry_to_game({"code": "038", "gid": "512050", "date": "2026-11-05",
                                         "teamLine": "VOLLEYBALL GIRLS JR. VARSITY", "text": "Vs. Lehman Campus @ Lehman Gym"})
        games, s = ps.merge({"meta": {"source": "psal"}, "games": [old]}, {"038/512050": new}, self.WINDOW, self.TODAY)
        self.assertEqual([g["id"] for g in games], ["psal-038-512050"])
        self.assertEqual(games[0]["score"], {"bxsci": 2, "opponent": 0})
        self.assertEqual(s["reidentified"], 1)


class EndToEndTests(unittest.TestCase):
    NOW = datetime(2026, 11, 10, 23, 30, tzinfo=TZ)

    def run_sync(self, fetcher, data_path, preview=False, prev_paths=None):
        args = SimpleNamespace(data=str(data_path), previous=prev_paths or [], preview=preview, no_details=False)
        with mock.patch.object(ps, "fetch_details", fake_details), \
                mock.patch.object(ps, "fetch_team_schedules", fake_team_schedules), \
                mock.patch.object(ps, "REPORT_DIR", Path(tempfile.mkdtemp())):
            return ps.run_sync(args, fetcher=fetcher, now=self.NOW)

    def test_full_sync_from_sample(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "games.json")
            p.write_text(json.dumps(sample_previous()))
            f = FakeFetcher()
            self.assertEqual(self.run_sync(f, p), 0)
            out = json.loads(p.read_text())
            self.assertEqual(out["meta"]["source"], "psal")
            self.assertEqual(out["meta"]["lastSynced"], "2026-11-10T23:30:00-05:00")
            self.assertNotIn("2026-11-11", out["meta"]["dayNotes"])         # sample notes removed
            ids = {g["id"] for g in out["games"]}
            self.assertFalse(any(not i.startswith("psal-") for i in ids))      # sample games removed
            self.assertEqual(out["meta"]["sync"]["window"], ["2026-09-01", "2027-01-31"])
            by = {g["id"]: g for g in out["games"]}
            vb = by["psal-016-511884"]
            self.assertEqual((vb["time"], vb["homeAway"], vb["status"], vb["score"]),
                             ("16:30", "home", "final", {"bxsci": 3, "opponent": 1}))
            self.assertEqual(by["psal-038-512001"]["status"], "postponed")
            self.assertEqual(by["psal-014-511990"]["homeAway"], "away")
            soc = by["psal-012-511700"]
            self.assertEqual((soc["status"], soc["score"]), ("scheduled", None))  # ambiguous -> Awaiting Result
            self.assertIn("psal-009-514000", ids)                            # January reached via postbacks
            # games PSAL lists beyond the school calendar
            self.assertEqual(by["psal-038-512050"]["score"], {"bxsci": 2, "opponent": 0})     # JV game + result
            self.assertEqual(by["psal-016-520001"]["round"], "Playoffs · Round of 32")       # playoff game
            self.assertEqual(by["psal-077-9900"]["event"], "Bronx Borough Championships")    # whole missing team
            self.assertIn("psal-012-511701", ids)                                            # doubleheader game
            self.assertNotIn("psal-055-888888", ids)                                         # duplicate merged
            self.assertEqual(by["psal-014-511990"]["score"], {"bxsci": 2, "opponent": 3})     # result from team page
            self.assertEqual(out["meta"]["sync"]["calendarGamesNotCollected"], 0)
            self.assertEqual(out["meta"]["sync"]["addedFromTeamSchedules"], 4)
            posts = [a for m, u, a in f.log if m == "POST"]
            self.assertEqual(posts, ["V_P11", "V_P10", "V_N11", "V_N12"])
            # sorted chronologically
            keys = [(g["date"], g["time"] or "99:99") for g in out["games"]]
            self.assertEqual(keys, sorted(keys))

    def test_outage_keeps_previous_file_exactly(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "games.json")
            original = json.dumps(sample_previous(), indent=2, ensure_ascii=False) + "\n"
            p.write_text(original)
            self.assertEqual(self.run_sync(FakeFetcher(fail="down"), p), 0)
            self.assertEqual(p.read_text(), original)

    def test_layout_change_is_an_error_and_keeps_data(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "games.json")
            original = json.dumps(sample_previous(), indent=2, ensure_ascii=False) + "\n"
            p.write_text(original)
            self.assertEqual(self.run_sync(FakeFetcher(fail="layout"), p), 1)
            self.assertEqual(p.read_text(), original)

    def test_robots_disallow_stops_before_collecting(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "games.json")
            p.write_text(json.dumps(sample_previous()))
            f = FakeFetcher(robots="User-agent: *\nDisallow: /events-and-standings/\n")
            self.assertEqual(self.run_sync(f, p), 1)
            self.assertFalse(any("school-calendar" in u for _, u, _ in f.log))
            self.assertEqual(json.loads(p.read_text())["meta"]["source"], "sample")

    def test_preview_does_not_touch_site_data(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "games.json")
            original = json.dumps(sample_previous())
            p.write_text(original)
            with mock.patch.object(ps, "PREVIEW_DIR", Path(d, "preview")):
                self.assertEqual(self.run_sync(FakeFetcher(), p, preview=True), 0)
            self.assertEqual(p.read_text(), original)
            self.assertEqual(json.loads(Path(d, "preview", "games.json").read_text())["meta"]["source"], "psal")

    def test_second_run_no_changes_and_detail_throttle(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d, "games.json")
            p.write_text(json.dumps(sample_previous()))
            self.run_sync(FakeFetcher(), p)
            first = json.loads(p.read_text())
            out_file = Path(d, "gh_out")
            with mock.patch.dict(os.environ, {"GITHUB_OUTPUT": str(out_file)}):
                self.run_sync(FakeFetcher(), p)
            self.assertIn("changed=false", out_file.read_text())
            second = json.loads(p.read_text())
            self.assertEqual(ps._comparable(first["games"]), ps._comparable(second["games"]))


class PlaywrightTeamScheduleTest(unittest.TestCase):
    """A JS team profile: FULL SCHEDULE tab must be clicked; practice tab + sidebar must not leak in."""
    PAGE = """<!doctype html><html><body>
<div id="tabs"><a href="#" onclick="show('full')">FULL SCHEDULE</a> <a href="#" onclick="show('prac')">PRACTICE SCHEDULE</a></div>
<div id="full" style="display:none"><table>
 <tr><th>Date</th><th>Opponent</th><th>Time</th><th>Result</th></tr>
 <tr><td>11/05/2026</td><td>@ Lehman Campus</td><td>4:00 PM</td><td>W 2-0</td><td><a href="https://www.psal.org/games/game-detail.aspx#038/512050">Details</a></td></tr>
 <tr><td>11/16/2026</td><td>PSAL Playoffs - Round of 32</td><td>vs. Midwood</td><td>4:00 PM</td><td><a href="https://www.psal.org/games/game-detail.aspx#038/520001">Details</a></td></tr>
</table></div>
<div id="prac" style="display:none"><table><tr><td>11/11/2026</td><td>Practice</td><td>3:30 PM</td></tr></table></div>
<aside><table><tr><td>11/11/2026</td><td>Hunter vs Beacon</td><td><a href="https://www.psal.org/games/game-detail.aspx#016/1">Details</a></td></tr></table></aside>
<script>function show(id){ setTimeout(function(){ document.getElementById(id).style.display='block'; }, 200); }</script>
</body></html>"""

    def test_render_and_parse(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "team.html").write_text(self.PAGE)
            handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=d, **k)  # noqa: E731
            srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            port = srv.server_address[1]
            f = ps.Fetcher(delay=0, debug_dir=None)
            f.robots = ps.RobotFileParser()
            f.robots.parse([])
            exe = os.environ.get("PW_CHROME")
            with mock.patch.object(ps, "team_profile_url", lambda c: f"http://127.0.0.1:{port}/team.html#{c}/10501"):
                if exe:
                    from playwright.sync_api import BrowserType
                    orig = BrowserType.launch
                    with mock.patch.object(BrowserType, "launch", lambda self, **kw: orig(self, executable_path=exe, **kw)):
                        res, warns = ps.fetch_team_schedules(f, ["038"], None)
                else:
                    res, warns = ps.fetch_team_schedules(f, ["038"], None)
            srv.shutdown()
        entries, skipped = ps.parse_team_rows(res["038"], "038", {"sport": "Volleyball", "gender": "Girls", "level": "JV"},
                                              date(2026, 11, 11))
        self.assertEqual(warns, [])
        self.assertEqual([(e["gid"], e["opponent"], e["result"], e["round"]) for e in entries],
                         [("512050", "Lehman Campus", {"bxsci": 2, "opponent": 0}, None),
                          ("520001", "Midwood", None, "Playoffs · Round of 32")])
        self.assertEqual(skipped, 1)        # the other schools' sidebar game; the hidden practice tab never appears


class PlaywrightDetailTest(unittest.TestCase):
    """Renders a JavaScript-filled page (like PSAL's game-detail) in real headless Chromium."""
    PAGE = """<!doctype html><html><body><h2>GAME/MEET RESULTS</h2>
<table><tr><td>Sport</td><td id="sp"></td></tr><tr><td>Status</td><td id="st"></td></tr>
<tr><td>Time</td><td id="tm"></td></tr><tr><td>Address</td><td id="ad"></td></tr></table><div id="res"></div>
<script>
setTimeout(function () {
  var h = location.hash.slice(1);
  document.getElementById('sp').textContent = 'VOLLEYBALL GIRLS V';
  document.getElementById('st').textContent = h === '016/1' ? 'Final' : 'Postponed';
  document.getElementById('tm').textContent = '4:30 PM';
  document.getElementById('ad').textContent = 'Bronx HS Of Science - GYM';
  if (h === '016/1') document.getElementById('res').innerHTML =
    '<div>The Bronx High School of Science 3</div><div>East Harlem Pride 1</div>';
}, 300);
</script></body></html>"""

    def test_render(self):
        try:
            import playwright  # noqa: F401
        except ImportError:
            self.skipTest("playwright not installed")
        with tempfile.TemporaryDirectory() as d:
            Path(d, "detail.html").write_text(self.PAGE)
            handler = lambda *a, **k: SimpleHTTPRequestHandler(*a, directory=d, **k)  # noqa: E731
            srv = ThreadingHTTPServer(("127.0.0.1", 0), handler)
            threading.Thread(target=srv.serve_forever, daemon=True).start()
            port = srv.server_address[1]
            f = ps.Fetcher(delay=0, debug_dir=None)
            f.robots = ps.RobotFileParser()
            f.robots.parse([])
            exe = os.environ.get("PW_CHROME")
            orig_launch = None
            with mock.patch.object(ps, "detail_url", lambda c, g: f"http://127.0.0.1:{port}/detail.html#{c}/{g}"):
                if exe:
                    from playwright.sync_api import BrowserType
                    orig_launch = BrowserType.launch
                    with mock.patch.object(BrowserType, "launch",
                                           lambda self, **kw: orig_launch(self, executable_path=exe, **kw)):
                        res, warns, _ = ps.fetch_details(f, [("016", "1", "East Harlem Pride"), ("016", "2", "X")], None)
                else:
                    res, warns, _ = ps.fetch_details(f, [("016", "1", "East Harlem Pride"), ("016", "2", "X")], None)
            srv.shutdown()
        self.assertEqual(warns, [])
        self.assertEqual(res["016/1"]["score"], {"bxsci": 3, "opponent": 1})
        self.assertEqual(res["016/1"]["time"], "16:30")
        self.assertEqual(res["016/2"]["status"], "Postponed")
        self.assertIsNone(res["016/2"]["score"])


if __name__ == "__main__":
    unittest.main()
