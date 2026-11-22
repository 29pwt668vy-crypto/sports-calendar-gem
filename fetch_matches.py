#!/usr/bin/env python3
"""
Sports Schedule -> iPhone Calendar (.ics) Sync Script
=====================================================
Fetches upcoming matches for:
  1. DEL2 (Germany Ice Hockey)
  2. Turkey BSL / Basketbol Süper Ligi (Basketball)
  3. Portugal Liga Betclic Masculina / LPB (Basketball)
  4. Portugal Liga Betclic Feminina (Basketball)

Uses API-Sports (https://api-sports.io/) free tier (100 requests/day per API)
and generates a standard RFC 5545 .ics calendar file with multi-day VALARM
notifications for Apple Calendar (iPhone).
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests

# ---------------------------------------------------------------------------
# Configuration (override via environment variables or GitHub Actions secrets)
# ---------------------------------------------------------------------------

API_SPORTS_KEY = os.getenv("API_SPORTS_KEY", "").strip()
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

# How many days back/ahead to include in the calendar
DAYS_BACK = int(os.getenv("DAYS_BACK", "2"))
DAYS_AHEAD = int(os.getenv("DAYS_AHEAD", "90"))

REQUEST_TIMEOUT = 20


@dataclass
class LeagueTarget:
    key: str
    label: str
    sport: str  # "hockey" or "basketball"
    base_url: str
    country: str
    name_keywords: tuple[str, ...]
    exclude_keywords: tuple[str, ...] = ()
    default_duration_minutes: int = 135


LEAGUE_TARGETS: list[LeagueTarget] = [
    LeagueTarget(
        key="del2",
        label="DEL2 (Хоккей)",
        sport="hockey",
        base_url="https://v1.hockey.api-sports.io",
        country="Germany",
        name_keywords=("del2", "del 2"),
        default_duration_minutes=150,
    ),
    LeagueTarget(
        key="turkey_bsl",
        label="Turkey BSL (Баскетбол)",
        sport="basketball",
        base_url="https://v1.basketball.api-sports.io",
        country="Turkey",
        name_keywords=("super lig", "süper lig", "bsl", "super ligi"),
        exclude_keywords=("women", "kadin", "tbl", "tb2l", "cup", "federation"),
        default_duration_minutes=120,
    ),
    LeagueTarget(
        key="portugal_lpb_men",
        label="Portugal Liga Betclic (Муж.)",
        sport="basketball",
        base_url="https://v1.basketball.api-sports.io",
        country="Portugal",
        name_keywords=("lpb", "liga betclic", "liga portuguesa"),
        exclude_keywords=("women", "feminina", "proliga", "cup", "taca", "taça"),
        default_duration_minutes=120,
    ),
    LeagueTarget(
        key="portugal_lpb_women",
        label="Portugal Liga Betclic Feminina (Жен.)",
        sport="basketball",
        base_url="https://v1.basketball.api-sports.io",
        country="Portugal",
        name_keywords=("feminina", "women"),
        exclude_keywords=("proliga", "cup", "taca", "taça", "divisao", "divisão"),
        default_duration_minutes=120,
    ),
]


@dataclass
class MatchEvent:
    uid: str
    league_label: str
    home_team: str
    away_team: str
    start_utc: datetime
    duration_minutes: int
    venue: str
    status: str


def api_get(base_url: str, endpoint: str, params: dict[str, Any]) -> dict[str, Any]:
    """Perform a GET request against API-Sports."""
    if not API_SPORTS_KEY:
        raise RuntimeError(
            "API_SPORTS_KEY is not set. Add it as an environment variable or GitHub Secret."
        )
    url = f"{base_url.rstrip('/')}/{endpoint.lstrip('/')}"
    headers = {"x-apisports-key": API_SPORTS_KEY}
    resp = requests.get(url, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    payload = resp.json()
    errors = payload.get("errors")
    if errors:
        raise RuntimeError(f"API-Sports error on {url}: {errors}")
    return payload


def pick_latest_season(seasons: list[dict[str, Any]]) -> str | int | None:
    """Pick the active or most recent season from an API-Sports league object."""
    if not seasons:
        return None
    now = datetime.now(timezone.utc).date()
    for s in seasons:
        start_str = s.get("start")
        end_str = s.get("end")
        if start_str and end_str:
            try:
                start_d = datetime.strptime(start_str[:10], "%Y-%m-%d").date()
                end_d = datetime.strptime(end_str[:10], "%Y-%m-%d").date()
                if start_d <= now <= end_d:
                    return s.get("season")
            except ValueError:
                pass
    # Fallback to the last season entry
    return seasons[-1].get("season")


def resolve_league(target: LeagueTarget) -> tuple[int, str | int] | None:
    """Resolve league ID and active season from API-Sports /leagues."""
    payload = api_get(target.base_url, "/leagues", {"country": target.country})
    candidates = payload.get("response", [])
    for item in candidates:
        # API-Hockey & API-Basketball both return league info either flat or nested
        league_obj = item.get("league", item)
        name = str(league_obj.get("name", "")).lower()
        if any(kw in name for kw in target.name_keywords) and not any(
            ex in name for ex in target.exclude_keywords
        ):
            league_id = league_obj.get("id")
            seasons = item.get("seasons", [])
            season = pick_latest_season(seasons)
            if league_id is not None and season is not None:
                return int(league_id), season
    return None


def parse_utc_datetime(raw_dt: str | None) -> datetime | None:
    if not raw_dt:
        return None
    cleaned = raw_dt.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(cleaned)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except ValueError:
        return None


def fetch_league_matches(target: LeagueTarget) -> list[MatchEvent]:
    """Fetch and normalize matches for a single LeagueTarget."""
    resolved = resolve_league(target)
    if not resolved:
        print(f"[WARN] Could not resolve league for {target.label}", file=sys.stderr)
        return []

    league_id, season = resolved
    print(f"[INFO] {target.label}: league_id={league_id}, season={season}")

    payload = api_get(
        target.base_url,
        "/games",
        {"league": league_id, "season": season},
    )
    games = payload.get("response", [])
    now = datetime.now(timezone.utc)
    window_start = now - timedelta(days=DAYS_BACK)
    window_end = now + timedelta(days=DAYS_AHEAD)

    events: list[MatchEvent] = []
    for g in games:
        start_utc = parse_utc_datetime(g.get("date"))
        if not start_utc or not (window_start <= start_utc <= window_end):
            continue

        teams = g.get("teams", {})
        home = str(teams.get("home", {}).get("name", "Home")).strip()
        away = str(teams.get("away", {}).get("name", "Away")).strip()

        if TEAM_FILTER:
            haystack = f"{home} {away}".lower()
            if not any(t in haystack for t in TEAM_FILTER):
                continue

        status_obj = g.get("status", {})
        status = str(
            status_obj.get("long") or status_obj.get("short") or "Scheduled"
        ).strip()
        venue = str(g.get("venue") or "").strip()
        game_id = g.get("id", f"{start_utc.timestamp()}-{home}-{away}")

        events.append(
            MatchEvent(
                uid=f"{target.key}-{game_id}@sports-sync",
                league_label=target.label,
                home_team=home,
                away_team=away,
                start_utc=start_utc,
                duration_minutes=target.default_duration_minutes,
                venue=venue,
                status=status,
            )
        )
    return events


def ics_escape(text: str) -> str:
    """Escape special characters according to RFC 5545."""
    return (
        text.replace("\\", "\\\\")
        .replace(";", "\\;")
        .replace(",", "\\,")
        .replace("\n", "\\n")
    )


def fold_ics_line(line: str) -> str:
    """Fold long lines at 75 octets per RFC 5545."""
    encoded = line.encode("utf-8")
    if len(encoded) <= 75:
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
    """Build a complete RFC 5545 iCalendar string with VALARM reminders."""
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
        summary = f"[{ev.league_label}] {ev.home_team} — {ev.away_team}"
        description = f"Лига: {ev.league_label}\\nМатч: {ev.home_team} vs {ev.away_team}\\nСтатус: {ev.status}"

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
                    f"DESCRIPTION:{ics_escape(f'Через {days} дн.: {ev.home_team} — {ev.away_team}')}",
                    f"TRIGGER:-P{days}D",
                    "END:VALARM",
                ]
            )

        lines.append("END:VEVENT")

    lines.append("END:VCALENDAR")
    return "\r\n".join(fold_ics_line(line) for line in lines) + "\r\n"


def main() -> int:
    all_events: list[MatchEvent] = []
    for target in LEAGUE_TARGETS:
        try:
            matches = fetch_league_matches(target)
            print(f"[INFO] Fetched {len(matches)} matches for {target.label}")
            all_events.extend(matches)
        except Exception as exc:
            print(f"[ERROR] Failed {target.label}: {exc}", file=sys.stderr)

    ics_content = build_ics(all_events)
    OUTPUT_ICS_PATH.parent.mkdir(parents=True, exist_ok=True)
    OUTPUT_ICS_PATH.write_text(ics_content, encoding="utf-8")
    print(f"[DONE] Wrote {len(all_events)} events to {OUTPUT_ICS_PATH}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())