"""World clock: which day of the world it is and which phase of the playbook that puts the bots in.

The playbook (plan //TRUENAS/Ai/Claude/plans/2026-11-13-smarter-bots.md, section 1):
  opening (days 1-4) -> second village (5-9) -> growth (10-39) -> relics (40-79) -> wonder (80+)
Relics appear on day RELICS_DAY and Wonder villages and building plans on WONDER_DAY (the game's NATARS_SPAWN_TIME
and NATARS_WW_SPAWN_TIME / NATARS_WW_BUILDING_PLAN_SPAWN_TIME: 40 and 80 since 2026-11-19, were 30 and 50).
Override with TRAVBOT_RELICS_DAY / TRAVBOT_WONDER_DAY when a world uses other values. The Etruscans attack a
Wonder by its level, not by day (travbot/wonder.py).

The day comes from the game ("Days since start" on statistiken.php?id=0, read at most every few hours), or from
TRAVBOT_WORLD_START (unix time of the world's start) when set.
"""
import os
import re
import time

from bs4 import BeautifulSoup

RELICS_DAY = int(os.environ.get("TRAVBOT_RELICS_DAY", "40"))
WONDER_DAY = int(os.environ.get("TRAVBOT_WONDER_DAY", "80"))
PHASES = ((WONDER_DAY, "wonder"), (RELICS_DAY, "relics"), (10, "growth"), (5, "second village"), (1, "opening"))
CHECK_EVERY_S = 3 * 3600


def phase(day):
    """Playbook phase for a world day (1 = the first day)."""
    if not day:
        return "growth"   # unknown: the middle of the road
    for start, name in PHASES:
        if day >= start:
            return name
    return "opening"


def parse_days_since_start(html):
    """'Days since start' tile on statistiken.php?id=0 -> int, or None."""
    soup = BeautifulSoup(html, "html.parser")
    for k in soup.select("span.tz-ally-k"):
        if "days since start" in k.get_text(strip=True).lower():
            v = k.find_next_sibling("span")
            m = re.search(r"\d+", v.get_text() if v else "")
            return int(m.group()) if m else None
    return None


class WorldClock:
    def __init__(self):
        self.start = None
        env = os.environ.get("TRAVBOT_WORLD_START", "").strip()
        if env.isdigit():
            self.start = int(env)
        self._day, self._checked = None, 0.0

    def day(self, client=None, now=None):
        """Day of the world, 1 = the first day; None if unknown."""
        now = now or time.time()
        if self.start:
            return int((now - self.start) // 86400) + 1 if now >= self.start else 0
        if client is not None and now - self._checked > CHECK_EVERY_S:
            self._checked = now
            try:
                days = parse_days_since_start(client.get("statistiken.php?id=0"))
                if days is not None:
                    self._day = days + 1
                    self._day_at = now
            except Exception:  # the clock must never stop the bot
                pass
        if self._day is None:
            return None
        return self._day + int((now - self._day_at) // 86400)

    def info(self, client=None):
        d = self.day(client)
        return {"day": d, "phase": phase(d), "relics_day": RELICS_DAY, "wonder_day": WONDER_DAY}
