"""
Soccer — live scores, fixtures and standings via ESPN's public JSON.

No API key. Endpoints used:
    https://site.api.espn.com/apis/site/v2/sports/soccer/{league}/scoreboard
    https://site.api.espn.com/apis/v2/sports/soccer/{league}/standings

Common league slugs:
    esp.1              La Liga            (Spain)
    eng.1              Premier League     (England)
    ita.1              Serie A            (Italy)
    ger.1              Bundesliga         (Germany)
    fra.1              Ligue 1            (France)
    usa.1              MLS                (United States)
    per.1              Liga 1             (Peru)
    uefa.champions     UEFA Champions League
    uefa.europa        UEFA Europa League
    conmebol.libertadores
"""
from __future__ import annotations

import logging
from typing import List, Optional, Tuple

LEAGUES = {
    "premier":            ("eng.1",              "Premier League"),
    "premier league":     ("eng.1",              "Premier League"),
    "epl":                ("eng.1",              "Premier League"),
    "england":            ("eng.1",              "Premier League"),
    "la liga":            ("esp.1",              "La Liga"),
    "laliga":             ("esp.1",              "La Liga"),
    "spain":              ("esp.1",              "La Liga"),
    "spanish":            ("esp.1",              "La Liga"),
    "serie a":            ("ita.1",              "Serie A"),
    "italy":              ("ita.1",              "Serie A"),
    "bundesliga":         ("ger.1",              "Bundesliga"),
    "germany":            ("ger.1",              "Bundesliga"),
    "ligue 1":            ("fra.1",              "Ligue 1"),
    "france":             ("fra.1",              "Ligue 1"),
    "mls":                ("usa.1",              "MLS"),
    "usa":                ("usa.1",              "MLS"),
    "peru":               ("per.1",              "Liga 1 Peru"),
    "peruvian":           ("per.1",              "Liga 1 Peru"),
    "liga 1":             ("per.1",              "Liga 1 Peru"),
    "champions":          ("uefa.champions",     "UEFA Champions League"),
    "champions league":   ("uefa.champions",     "UEFA Champions League"),
    "ucl":                ("uefa.champions",     "UEFA Champions League"),
    "europa":             ("uefa.europa",        "UEFA Europa League"),
    "libertadores":       ("conmebol.libertadores", "Copa Libertadores"),
    "world cup":          ("fifa.world",         "FIFA World Cup"),
}
DEFAULT_LEAGUE_SLUG = "esp.1"
DEFAULT_LEAGUE_NAME = "La Liga"


def resolve_league(query: Optional[str]) -> Tuple[str, str]:
    """Map free-form league text to an ESPN slug + friendly name."""
    if not query:
        return DEFAULT_LEAGUE_SLUG, DEFAULT_LEAGUE_NAME
    q = query.lower().strip()
    # longest-key first so "champions league" beats "champions"
    for key in sorted(LEAGUES, key=len, reverse=True):
        if key in q:
            return LEAGUES[key]
    return DEFAULT_LEAGUE_SLUG, DEFAULT_LEAGUE_NAME


def _get(url: str, timeout: float = 6.0):
    import requests
    # ESPN's public JSON blocks non-standard User-Agents; keep the default.
    r = requests.get(url, timeout=timeout)
    r.raise_for_status()
    return r.json()


def scoreboard(league_slug: str = DEFAULT_LEAGUE_SLUG) -> List[dict]:
    """Return a compact list of today's matches for a league."""
    try:
        data = _get(
            f"https://site.api.espn.com/apis/site/v2/sports/soccer/{league_slug}/scoreboard"
        )
    except Exception as exc:
        logging.warning("scoreboard fetch failed for %s: %s", league_slug, exc)
        return []

    out = []
    for ev in data.get("events", []):
        try:
            comp = ev["competitions"][0]
            teams = comp["competitors"]
            home = next(c for c in teams if c["homeAway"] == "home")
            away = next(c for c in teams if c["homeAway"] == "away")
            state = ev.get("status", {}).get("type", {}).get("state", "pre")
            out.append({
                "state":  state,  # pre | in | post
                "status": comp["status"]["type"]["shortDetail"],
                "home":   home["team"]["shortDisplayName"],
                "away":   away["team"]["shortDisplayName"],
                "home_score": home.get("score", "-"),
                "away_score": away.get("score", "-"),
                "start":  ev.get("date", ""),
            })
        except Exception as exc:
            logging.debug("skip event: %s", exc)
    return out


def format_scoreboard(games: List[dict], league_name: str) -> Tuple[str, str]:
    """Return (spoken_summary, printable_block)."""
    if not games:
        spoken = f"No {league_name} matches on the schedule right now."
        return spoken, f"[jarvis] {spoken}"

    live = [g for g in games if g["state"] == "in"]
    done = [g for g in games if g["state"] == "post"]
    upcoming = [g for g in games if g["state"] == "pre"]

    lines = [f"── {league_name} ──"]
    for label, bucket in (("LIVE", live), ("Final", done), ("Upcoming", upcoming)):
        if not bucket:
            continue
        lines.append(f"  {label}:")
        for g in bucket:
            score = f"{g['away_score']}-{g['home_score']}" if label != "Upcoming" else "vs"
            lines.append(f"    {g['away']:<18} {score:^5} {g['home']:<18}  ({g['status']})")

    # Spoken summary — keep it short and readable
    if live:
        spoken_bits = [
            f"{g['away']} {g['away_score']}, {g['home']} {g['home_score']}"
            for g in live[:3]
        ]
        spoken = f"Live in {league_name}: " + "; ".join(spoken_bits) + "."
    elif done:
        spoken_bits = [
            f"{g['away']} {g['away_score']}, {g['home']} {g['home_score']}"
            for g in done[:3]
        ]
        spoken = f"Latest {league_name} results: " + "; ".join(spoken_bits) + "."
    else:
        spoken = f"{len(upcoming)} upcoming {league_name} matches. Next: {upcoming[0]['away']} versus {upcoming[0]['home']}."

    return spoken, "\n".join(lines)
