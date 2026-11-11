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


def sport_page(items):
    """items: (code, gid, team, mmddyyyy, time, body, venue) like PSAL's list."""
    lis = []
    for code, gid, team, d, t, body, venue in items:
        lis.append(f"""<li>Bronx &nbsp;&nbsp; <a href="https://www.psal.org/games/game-detail.aspx#{code}/{gid}">Details &gt;&gt;</a>
<br/>{team}<br/>{d} {t}<br/>{body} @ {venue} <a href="#">map</a> 297</li>""")
    return f"<html><body><h1>PSAL</h1><ul>{''.join(lis)}</ul><table><tr><td>Bronx Science</td><td>wolverines</td></tr></table></body></html>"


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
       date(2026, 9, 30): [("SOCCER BOYS VARSITY", "Vs. Lehman Campus @ Macombs Dam Park: Soccer-01", "012", "510001")]}
DEC = {date(2026, 12, 2): NOV[date(2026, 12, 2)],
       date(2026, 12, 9): [("TABLE TENNIS BOYS VARSITY", "Vs. Adlai Stevenson Campus @ Bronx HS of Science - Table Tennis", "065", "513000")]}
SEP = {date(2026, 9, 30): OCT[date(2026, 9, 30)],
       date(2026, 9, 15): [("SOCCER GIRLS VARSITY", "Vs. Lehman Campus @ Macombs Dam Park: Soccer-01", "012", "509001")]}
JAN = {date(2027, 1, 6): [("GYMNASTICS GIRLS VARSITY", "Vs. DeWitt Clinton High School @ Dewitt Clinton-Gym", "009", "514000")]}

SPORT_PAGES = {
    "016": sport_page([("016", "511884", "VOLLEYBALL GIRLS V", "11/09/2026", "4:30 PM",
                        "The Bronx High School of Science vs East Harlem Pride", "Bronx HS Of Science - GYM"),
                       ("016", "599999", "VOLLEYBALL GIRLS V", "11/09/2026", "4:30 PM",
                        "John Jay Campus vs Tottenville High School", "John Jay Campus - GYM")]),
    "014": sport_page([("014", "511990", "TENNIS GIRLS V", "11/10/2026", "5:00 pm",
                        "Townsend Harris High School vs The Bronx High School of Science", "Queens College Tennis #1")]),
    "038": sport_page([("038", "512001", "VOLLEYBALL GIRLS JV", "11/12/2026", "04:00 PM",
                        "postponed early The Bronx High School of Science vs Bronx United", "Bronx HS Of Science - GYM")]),
    "012": sport_page([("012", "511700", "SOCCER GIRLS V", "11/02/2026", "2:30 PM",
                        "The Bronx High School of Science vs Stuyvesant High School", "Harris Park: Soccer-01")]),
}

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


class EndToEndTests(unittest.TestCase):
    NOW = datetime(2026, 11, 10, 23, 30, tzinfo=TZ)

    def run_sync(self, fetcher, data_path, preview=False, prev_paths=None):
        args = SimpleNamespace(data=str(data_path), previous=prev_paths or [], preview=preview, no_details=False)
        with mock.patch.object(ps, "fetch_details", fake_details):
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
