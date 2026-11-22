#!/usr/bin/env python3
"""
Sports Schedule -> iPhone Calendar (.ics) Sync Script
=====================================================
Fetches upcoming matches for:
  1. DEL2 (Germany Ice Hockey) -> Official DEL2 ICS / schedule (no API key needed)
  2. Portugal Liga Betclic Masculina (Basketball) -> Official FPB calendar (no API key needed)
  3. Portugal Liga Betclic Feminina (Basketball) -> Official FPB calendar (no API key needed)
  4. Turkey BSL / Basketbol Süper Ligi (Basketball) -> Highlightly Basketball API (free tier, 100 req/day)

Generates a standard RFC 5545 .ics calendar file with multi-day VALARM
notifications for Apple Calendar (iPhone).
"""

from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urljoin
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

# ---------------------------------------------------------------------------
# Configuration (override via environment variables or GitHub Actions secrets)
# ---------------------------------------------------------------------------

HIGHLIGHTLY_API_KEY = os.getenv("HIGHLIGHTLY_API_KEY", "").strip()
OUTPUT_ICS_PATH = Path(os.getenv("OUTPUT_ICS_PATH", "matches.ics"))

# Comma-separated reminder offsets in days before match start (e.g. "3,1" = 3 days & 1 day before)
REMINDER_DAYS = [
    int(d.strip())
    for d in os.getenv("REMINDER_DAYS", "3,1").split(",")
    if d.strip().isdigit()
]

# Optional comma-separated team name substrings to filter (leave empty for all teams)
TEAM_FILTER = [
    t.strip().lower()
    for t in os.getenv("TEAM_FILTER", "").split(",")
    if t.strip()
]

DAYS_BACK = int(os.getenv("DAYS_BACK", "2"))
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "90"))
REQUEST_TIMEOUT = 25

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (compatible; SportsCalendarSync/1.0; +https://github.com/)"
    )
}

PT_MONTHS = {
    "JAN": 1,
    "FEV": 2,
    "MAR": 3,
    "ABR": 4,
    "MAI": 5,
    "JUN": 6,
    "JUL": 7,
    "AGO": 8,
    "SET": 9,
    "OUT": 10,
    "NOV": 11,
    "DEZ": 12,
}


@dataclass
class MatchEvent:
    uid: str
    league_label: str
    home_team: str
    away_team: str
    start_utc: datetime
    duration_minutes: int
    venue: str = ""
    status: str = "Scheduled"


def in_window(dt_utc: datetime) -> bool:
    now = datetime.now(timezone.utc)
    return (now - timedelta(days=DAYS_BACK)) <= dt_utc <= (
        now + timedelta(days=DAYS_AHEAD)
    )


def matches_team_filter(home: str, away: str) -> bool:
    if not TEAM_FILTER:
        return True
    haystack = f"{home} {away}".lower()
    return any(t in haystack for t in TEAM_FILTER)


# ---------------------------------------------------------------------------
# 1. DEL2 (Germany Ice Hockey) — Official DEL2 ICS / Schedule (No API Key)
# ---------------------------------------------------------------------------


def parse_ics_datetime(raw: str, tzid: str | None = None) -> datetime | None:
    raw = raw.strip()
    try:
        if raw.endswith("Z"):
            return datetime.strptime(raw, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        if "T" in raw:
            dt = datetime.strptime(raw[:15], "%Y%m%dT%H%M%S")
            tz = ZoneInfo(tzid) if tzid else ZoneInfo("Europe/Berlin")
            return dt.replace(tzinfo=tz).astimezone(timezone.utc)
    except Exception:
        return None
    return None


def fetch_del2_official() -> list[MatchEvent]:
    """Discover and parse the official DEL2 full-season .ics feed from del-2.org/kalender/."""
    cal_page = "https://www.del-2.org/kalender/"
    resp = requests.get(cal_page, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()

    soup = BeautifulSoup(resp.text, "html.parser")
    ics_url = None
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if ".ics" in href.lower():
            # Prefer the full-league feed ("Alle Spiele" / first general ics link)
            ics_url = urljoin(cal_page, href.replace("webcal://", "https://"))
            if "alle" in (a.get_text(" ", strip=True) + href).lower():
                break

    if not ics_url:
        print("[WARN] DEL2: .ics link not found on del-2.org/kalender/, trying spielplan", file=sys.stderr)
        return fetch_del2_spielplan_fallback()

    ics_resp = requests.get(ics_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    ics_resp.raise_for_status()

    # Unfold RFC 5545 folded lines
    raw_text = ics_resp.text.replace("\r\n ", "").replace("\r\n\t", "")
    events: list[MatchEvent] = []
    in_event = False
    cur: dict[str, tuple[str, str | None]] = {}

    for line in raw_text.splitlines():
        line = line.strip()
        if line == "BEGIN:VEVENT":
            in_event = True
            cur = {}
            continue
        if line == "END:VEVENT" and in_event:
            in_event = False
            dt_val, tzid = cur.get("DTSTART", ("", None))
            start_utc = parse_ics_datetime(dt_val, tzid)
            summary = cur.get("SUMMARY", ("", None))[0]
            if not start_utc or not in_window(start_utc) or not summary:
                continue

            parts = re.split(r"\s+(?:-|–|—|vs\.?|:)\s+", summary, maxsplit=1)
            if len(parts) == 2:
                home, away = parts[0].strip(), parts[1].strip()
            else:
                home, away = summary.strip(), ""

            if not matches_team_filter(home, away):
                continue

            uid = cur.get("UID", (f"del2-{int(start_utc.timestamp())}-{home}", None))[0]
            venue = cur.get("LOCATION", ("", None))[0]
            events.append(
                MatchEvent(
                    uid=f"del2-{uid}",
                    league_label="DEL2 (Хоккей)",
                    home_team=home,
                    away_team=away,
                    start_utc=start_utc,
                    duration_minutes=150,
                    venue=venue,
                )
            )
            continue

        if in_event and ":" in line:
            key_part, val = line.split(":", 1)
            key_name = key_part.split(";", 1)[0].upper()
            tzid = None
            m_tz = re.search(r"TZID=([^;:]+)", key_part, re.IGNORECASE)
            if m_tz:
                tzid = m_tz.group(1)
            cur[key_name] = (val.strip(), tzid)

    return events


def fetch_del2_spielplan_fallback() -> list[MatchEvent]:
    """Fallback parser for https://www.del-2.org/spielplan/."""
    url = "https://www.del-2.org/spielplan/"
    resp = requests.get(url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    text = BeautifulSoup(resp.text, "html.parser").get_text("\n")
    berlin = ZoneInfo("Europe/Berlin")
    now_berlin = datetime.now(berlin)
    events: list[MatchEvent] = []

    # Matches lines like: "ECK - : - EBR" followed by "27.11. 19:30 Uhr"
    pattern = re.compile(
        r"([A-Z0-9]{2,5})\s*-\s*:\s*-\s*([A-Z0-9]{2,5})\s+(\d{2})\.(\d{2})\.\s+(\d{2}):(\d{2})\s*Uhr"
    )
    for m in pattern.finditer(text):
        home, away, day, month, hour, minute = m.groups()
        year = now_berlin.year
        if int(month) < now_berlin.month - 2:
            year += 1
        dt_local = datetime(year, int(month), int(day), int(hour), int(minute), tzinfo=berlin)
        start_utc = dt_local.astimezone(timezone.utc)
        if not in_window(start_utc) or not matches_team_filter(home, away):
            continue
        events.append(
            MatchEvent(
                uid=f"del2-{int(start_utc.timestamp())}-{home}-{away}@sports-sync",
                league_label="DEL2 (Хоккей)",
                home_team=home,
                away_team=away,
                start_utc=start_utc,
                duration_minutes=150,
            )
        )
    return events


# ---------------------------------------------------------------------------
# 2. Portugal Liga Betclic (Men & Women) — Official FPB Calendar (No API Key)
# ---------------------------------------------------------------------------


def fetch_fpb_portugal() -> list[MatchEvent]:
    """Parse upcoming Liga Betclic Masculina & Feminina games from fpb.pt/calendario/."""
    lisbon = ZoneInfo("Europe/Lisbon")
    events: list[MatchEvent] = []
    seen_uids: set[str] = set()

    # Check main calendar page + Liga Betclic Feminina page
    urls = [
        "https://www.fpb.pt/calendario/",
        "https://www.fpb.pt/competicao/10906/",
    ]

    date_re = re.compile(r"\b(\d{1,2})\s+([A-Z]{3})\s+(\d{4})\b")
    time_re = re.compile(r"\b(\d{2})H(\d{2})\b", re.IGNORECASE)

    for page_url in urls:
        try:
            resp = requests.get(page_url, headers=HEADERS, timeout=REQUEST_TIMEOUT)
            resp.raise_for_status()
        except Exception as exc:
            print(f"[WARN] FPB fetch failed for {page_url}: {exc}", file=sys.stderr)
            continue

        soup = BeautifulSoup(resp.text, "html.parser")
        lines = [ln.strip() for ln in soup.get_text("\n").splitlines() if ln.strip()]

        current_date: tuple[int, int, int] | None = None
        i = 0
        while i < len(lines):
            dm = date_re.fullmatch(lines[i].upper())
            if dm:
                d_str, m_str, y_str = dm.groups()
                if m_str in PT_MONTHS:
                    current_date = (int(y_str), PT_MONTHS[m_str], int(d_str))
                i += 1
                continue

            tm = time_re.fullmatch(lines[i])
            if tm and current_date and i >= 1:
                hour, minute = int(tm.group(1)), int(tm.group(2))
                home = lines[i - 1]
                # Look ahead within next 6 lines for away team, venue, and competition name
                window_lines = lines[i + 1 : i + 7]
                comp_label = None
                for wl in window_lines:
                    low = wl.lower()
                    if "liga betclic feminina" in low:
                        comp_label = "Portugal Liga Betclic Feminina (Жен.)"
                        break
                    if "liga betclic masculina" in low:
                        comp_label = "Portugal Liga Betclic (Муж.)"
                        break

                if comp_label and len(window_lines) >= 1:
                    away = window_lines[0]
                    venue = window_lines[1] if len(window_lines) >= 2 and "liga betclic" not in window_lines[1].lower() else ""
                    dt_local = datetime(
                        current_date[0],
                        current_date[1],
                        current_date[2],
                        hour,
                        minute,
                        tzinfo=lisbon,
                    )
                    start_utc = dt_local.astimezone(timezone.utc)
                    uid = f"fpb-{int(start_utc.timestamp())}-{home}-{away}@sports-sync"
                    if uid not in seen_uids and in_window(start_utc) and matches_team_filter(home, away):
                        seen_uids.add(uid)
                        events.append(
                            MatchEvent(
                                uid=uid,
                                league_label=comp_label,
                                home_team=home,
                                away_team=away,
                                start_utc=start_utc,
                                duration_minutes=120,
                                venue=venue,
                            )
                        )
            i += 1

    return events


# ---------------------------------------------------------------------------
# 3. Turkey BSL (Basketball) — Highlightly Basketball API (Free Plan: 100/day)
# ---------------------------------------------------------------------------


def fetch_turkey_bsl_highlightly() -> list[MatchEvent]:
    """Fetch Turkey BSL upcoming matches via Highlightly Basketball API (free tier)."""
    if not HIGHLIGHTLY_API_KEY:
        print(
            "[INFO] HIGHLIGHTLY_API_KEY is not set — skipping Turkey BSL. "
            "Get a free key (100 req/day, no card) at https://highlightly.net/basketball-api/",
            file=sys.stderr,
        )
        return []

    base_url = "https://basketball.highlightly.net"
    hl_headers = {"x-rapidapi-key": HIGHLIGHTLY_API_KEY}

    # 1. Discover Turkish Super Ligi ID
    leagues_resp = requests.get(
        f"{base_url}/leagues",
        headers=hl_headers,
        params={"countryCode": "TR", "limit": 50},
        timeout=REQUEST_TIMEOUT,
    )
    leagues_resp.raise_for_status()
    leagues_data = leagues_resp.json().get("data", [])

    league_id = None
    season = None
    for lg in leagues_data:
        name = str(lg.get("name", "")).lower()
        if ("super" in name or "bsl" in name) and "women" not in name and "kadin" not in name:
            league_id = lg.get("id")
            seasons = lg.get("seasons", [])
            if seasons:
                last_s = seasons[-1]
                season = last_s.get("season") if isinstance(last_s, dict) else last_s
            break

    if not league_id:
        print("[WARN] Turkey BSL: could not find league ID in Highlightly /leagues", file=sys.stderr)
        return []

    # 2. Fetch matches for the league & season
    params: dict[str, Any] = {"leagueId": league_id, "limit": 100}
    if season:
        params["season"] = season

    matches_resp = requests.get(
        f"{base_url}/matches",
        headers=hl_headers,
        params=params,
        timeout=REQUEST_TIMEOUT,
    )
    matches_resp.raise_for_status()
    matches_data = matches_resp.json().get("data", [])

    events: list[MatchEvent] = []
    for m in matches_data:
        raw_date = m.get("date") or m.get("startTime")
        if not raw_date:
            continue
        try:
            start_utc = datetime.fromisoformat(str(raw_date).replace("Z", "+00:00")).astimezone(timezone.utc)
        except ValueError:
            continue

        if not in_window(start_utc):
            continue

        home = str(m.get("homeTeam", {}).get("name", "Home")).strip()
        away = str(m.get("awayTeam", {}).get("name", "Away")).strip()
        if not matches_team_filter(home, away):
            continue

        state_desc = str(m.get("state", {}).get("description", "Scheduled")).strip()
        venue = ""
        if isinstance(m.get("venue"), dict):
            venue = str(m["venue"].get("name") or "").strip()

        events.append(
            MatchEvent(
                uid=f"bsl-{m.get('id', int(start_utc.timestamp()))}@sports-sync",
                league_label="Turkey BSL (Баскетбол)",
                home_team=home,
                away_team=away,
                start_utc=start_utc,
                duration_minutes=120,
                venue=venue,
                status=state_desc,
            )
        )
    return events


# ---------------------------------------------------------------------------
# ICS Builder (RFC 5545 with VALARM reminders for iPhone)
# ---------------------------------------------------------------------------


def ics_escape(text: str) -> str:
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_ics_line(line: str) -> str:
    if len(line.encode("utf-8")) <= 75:
        return line
    chunks: list[str] = []
    current = ""
    for ch in line:
        if len((current + ch).encode("utf-8")) > 75:
            chunks.append(current)
            current = " " + ch
        else:
            current += ch
    if current:
        chunks.append(current)
    return "\r\n".join(chunks)


def build_ics(events: list[MatchEvent]) -> str:
    now_stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = [
        "BEGIN:VCALENDAR",
        "VERSION:2.0",
        "PRODID:-//SportsMatchSync//iPhone Calendar//RU",
        "CALSCALE:GREGORIAN",
        "METHOD:PUBLISH",
        "X-WR-CALNAME:DEL2 / Turkey BSL / Portugal Betclic",
        "X-WR-TIMEZONE:UTC",
        "REFRESH-INTERVAL;VALUE=DURATION:PT12H",
        "X-PUBLISHED-TTL:PT12H",
    ]

    for ev in sorted(events, key=lambda e: e.start_utc):
        dt_start = ev.start_utc.strftime("%Y%m%dT%H%M%SZ")
        dt_end = (ev.start_utc + timedelta(minutes=ev.duration_minutes)).strftime(
            "%Y%m%dT%H%M%SZ"
        )
        title = f"{ev.home_team} — {ev.away_team}" if ev.away_team else ev.home_team
        summary = f"[{ev.league_label}] {title}"
        description = f"Лига: {ev.league_label}\\nМатч: {title}\\nСтатус: {ev.status}"

        lines.extend(
            [
                "BEGIN:VEVENT",
                f"UID:{ics_escape(ev.uid)}",
                f"DTSTAMP:{now_stamp}",
                f"DTSTART:{dt_start}",
                f"DTEND:{dt_end}",
                f"SUMMARY:{ics_escape(summary)}",
                f"DESCRIPTION:{description}",
            ]
        )
        if ev.venue:
            lines.append(f"LOCATION:{ics_escape(ev.venue)}")

        for days in REMINDER_DAYS:
            lines.extend(
                [
                    "BEGIN:VALARM",
                    "ACTION:DISPLAY",
                    f"DESCRIPTION:{ics_escape(f'Через {days} дн.: {title}')}",
                    f"TRIGGER:-P{days}D",
                    "END:VALARM",
                ]
            )

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def main() -> int:
    all_events: list[MatchEvent] = []

    sources = [
        ("DEL2 (Хоккей)", fetch_del2_official),
        ("Portugal Liga Betclic (Муж. и Жен.)", fetch_fpb_portugal),
        ("Turkey BSL (Баскетбол)", fetch_turkey_bsl_highlightly),
    ]

    for label, fn in sources:
        try:
            items = fn()
            print(f"[INFO] {label}: fetched {len(items)} matches")
            all_events.extend(items)
        except Exception as exc:
            print(f"[ERROR] {label}: {exc}", file=sys.stderr)

    ics_content = build_ics(all_events)
    OUTPUT_ICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_ICS_PATH.write_text(ics_content, encoding="utf-8")
    print(f"[DONE] Wrote {len(all_events)} events to {OUTPUT_ICS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())