"""Three champions, three jobs (Brandon 2026-11-13).

The game allows 1 champion with a Champion's Hall, 2 at Hall level 10 and 3 at Hall level 20 (highest Hall of the
account), one per unit type. A champion is made in a Champion's Hall from a unit at home there; it lives in that
village and moves home when sent alone as a reinforcement to an own village with a Champion's Hall.

  war       made first, lives in the army village. The tribe's best attacking cavalry. Stats: Might + Command.
            Marches with counter-strikes on players who attacked the alliance and clears oases of animals
            (loot, experience, and annexing oases next to the army village). Never on Etruscan relic villages:
            a champion in such an attack would take the relic, and relics are the human members' to take.
  steward   made at Hall 10, moves to the capital. A fast cavalry unit (short quests). Stats: Stewardship +
            Insight; as the capital's active champion it raises the capital's resources and gold. Short quests only.
  guardian  made at Hall 20, moves to the bot village attacked most in the last week (else the one nearest the
            alliance's Wonder, else the biggest feeder). The best defensive unit. Stats: Fortitude + Vitality; as
            that village's active champion it raises its defenders' strength. Short quests only, none while the
            alliance is under attack.
All three are trained in the army village; the units they are made from are trained there first.
"""
import json
import logging
import math
import time

from . import combat, rules
from .champion import page_data

log = logging.getLogger("travbot")

JOBS = ("war", "steward", "guardian")
ROLE = {"war": "attack", "steward": "economy", "guardian": "defence"}
# unit preferences per tribe, best first (one champion per unit type, so later entries are fallbacks)
UNITS = {
    "war":      {1: [6, 5, 3], 2: [16, 13, 15], 3: [26, 24, 22]},
    "steward":  {1: [5, 6, 3], 2: [15, 16, 13], 3: [24, 26, 25]},
    "guardian": {1: [2, 1, 3], 2: [12, 11, 15], 3: [25, 21, 22]},
}
HALL_FOR = {"war": 1, "steward": 10, "guardian": 20}   # Hall level the account needs for the 1st/2nd/3rd champion
OASIS_COOLDOWN_S = 12 * 3600


def hall_slot(state):
    return rules.find_slot(state, "Hero's Mansion")


class ChampionPlan:
    def __init__(self, client, tribe, state_dir, dry_run):
        self.c = client
        self.tribe = tribe
        self.dry_run = dry_run
        self.path = state_dir / "champions.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("jobs", {})        # heroid -> job
        self.data.setdefault("oases", {})       # wref -> last clearing time
        self.data.setdefault("attacked", {})    # own village wref -> [times of attacks]
        self.champs = {}                        # job -> {"heroid", "unit", "home" (bool), "wref", "health"}

    def save(self):
        if not self.dry_run:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
            tmp.replace(self.path)

    # --- what we have -----------------------------------------------------------------------------

    def refresh(self, hall_village, slot):
        """Read the account's champions from the Champion's Hall in `hall_village` (selected by the caller)."""
        first = page_data(self.c.get(f"build.php?id={slot}&t4tab=items"), "tz-champ")
        self.champs = {}
        if not first or not first.get("hero"):
            return 0
        listed = first.get("champions") or [{"heroid": first["hero"]["heroid"], "dead": False}]
        for ch in listed:
            if ch.get("dead"):
                continue
            inv = page_data(self.c.get(f"build.php?id={slot}&champ={ch['heroid']}&t4tab=items"), "tz-champ")
            hero = (inv or {}).get("hero")
            if not hero:
                continue
            job = self.data["jobs"].get(str(hero["heroid"])) or self._job_for_unit(hero["unit"])
            if job is None or job in self.champs:
                continue
            self.data["jobs"][str(hero["heroid"])] = job
            self.champs[job] = {"heroid": hero["heroid"], "unit": hero["unit"], "home": hero.get("home"),
                                "wref": hero.get("wref"), "health": hero.get("health", 0)}
        return len(listed)

    def _job_for_unit(self, unit):
        taken = set(self.champs)
        for job in JOBS:
            if job not in taken and unit in UNITS[job].get(self.tribe, []):
                return job
        return next((j for j in JOBS if j not in taken), None)

    def roles(self):
        """heroid -> stat role, for the champion keeper."""
        return {c["heroid"]: ROLE[job] for job, c in self.champs.items()}

    def quiet_jobs(self):
        return {c["heroid"]: job for job, c in self.champs.items() if job != "war"}

    def war_champion(self, at_wref=None):
        """heroid of the war champion if it is at home (in `at_wref` when given), else None."""
        w = self.champs.get("war")
        if not w or not w["home"] or w["health"] < 40 or (at_wref is not None and w["wref"] != at_wref):
            return None
        return w["heroid"]

    # --- which job next, and from which unit ----------------------------------------------------------

    def next_job(self, best_hall, phase):
        if phase in ("opening", "second village"):
            return None
        for job in JOBS:
            if job not in self.champs:
                return job if best_hall >= HALL_FOR[job] else None
        return None

    def wanted_units(self, best_hall, phase):
        """{unit id: 1} the army village should have at home so the next champion can be made from it."""
        job = self.next_job(best_hall, phase)
        if job is None:
            return {}
        used = {c["unit"] for c in self.champs.values()}
        u = next((u for u in UNITS[job].get(self.tribe, []) if u not in used), None)
        return {u: 1} if u else {}

    # --- per round -----------------------------------------------------------------------------------

    def note_attacks(self, feed, own_wrefs, now=None):
        """Remember attacks on our own villages (the guardian goes where the fighting is)."""
        now = now or time.time()
        for inc in (feed or {}).get("incoming", []):
            if inc.get("to") in own_wrefs and inc.get("from_owner") not in (0, 2, 3):
                times = self.data["attacked"].setdefault(str(inc["to"]), [])
                if inc.get("arrives") not in times:
                    times.append(inc.get("arrives"))
        for k in list(self.data["attacked"]):
            self.data["attacked"][k] = [t for t in self.data["attacked"][k] if t and now - t < 7 * 86400][-50:]
            if not self.data["attacked"][k]:
                del self.data["attacked"][k]

    def guardian_village(self, villages, roles, coords, wonders):
        """Village id the guardian should live in."""
        cands = [v for v in villages if roles.get(v["id"]) != "capital"]
        if not cands:
            return None
        hits = {int(k): len(t) for k, t in self.data["attacked"].items()}
        hit = max(cands, key=lambda v: hits.get(v["id"], 0))
        if hits.get(hit["id"], 0) > 0:
            return hit["id"]
        if wonders:
            def d(v):
                c = coords.get(v["id"])
                return min(math.hypot(w["x"] - c[0], w["y"] - c[1]) for w in wonders) if c else 1e9
            return min(cands, key=d)["id"]
        feeders = [v for v in cands if roles.get(v["id"]) == "feeder"] or cands
        return max(feeders, key=lambda v: rules.total_production(v["state"]))["id"]

    def step(self, villages, roles, coords, phase, capital, guardian_vid):
        """Train the next champion in the army village and move the steward/guardian home. Returns actions."""
        actions = []
        army = next((v for v in villages if roles.get(v["id"]) == "army" and hall_slot(v["state"])), None)
        best_hall = max((rules.building_level(v["state"], "Hero's Mansion") for v in villages), default=0)
        if army is None:
            return actions
        if army["id"] is not None:
            self.c.select_village(army["id"])
        slot = hall_slot(army["state"])
        count = self.refresh(army, slot)
        job = self.next_job(best_hall, phase)
        if job is not None:
            actions += self._train(army, slot, job, count)
        homes = {"steward": capital, "guardian": guardian_vid}
        for job, vid in homes.items():
            ch = self.champs.get(job)
            if not ch or vid is None or not ch["home"] or ch["wref"] == vid:
                continue
            target = next((v for v in villages if v["id"] == vid), None)
            c = coords.get(vid)
            if target is None or not c or not hall_slot(target["state"]):
                continue   # the role targets give it a Champion's Hall first
            actions += self._move(ch, job, target, c)
        self.save()
        return actions

    def _train(self, army, slot, job, count):
        used = {c["unit"] for c in self.champs.values()}
        prefs = [u for u in UNITS[job].get(self.tribe, []) if u not in used]
        if not prefs:
            return []
        page = self.c.get(f"build.php?id={slot}" + ("&champ=new" if count else ""))
        for u in prefs:
            if army["state"]["troops"].get(u, 0) < 1:
                continue
            marker = f"train={u}"
            href = next((h for h in _links(page) if marker in h and h.rstrip("&").endswith(marker)), None)
            if not href:
                continue
            log.info("champion: %smake the %s champion from a %s in %s", "[dry-run] " if self.dry_run else "", job,
                     _uname(u), army["name"])
            if not self.dry_run:
                self.c.get(href)
            return [{"action": "champion", "name": f"{job} champion ({_uname(u)})", "village": army["name"],
                     "reason": "new champion", "time": int(time.time())}]
        return []

    def _move(self, ch, job, target, c):
        log.info("champion: %ssend the %s champion home to %s", "[dry-run] " if self.dry_run else "", job, target["name"])
        if self.dry_run:
            return [{"action": "champion", "name": f"{job} champion moves to {target['name']}", "village": "",
                     "reason": "new home", "time": int(time.time())}]
        self.c.select_village(ch["wref"])
        r = self.c.send_army(c[0], c[1], 2, {}, champ_id=ch["heroid"])
        if not r["ok"]:
            log.info("champion: the game refused moving the %s champion: %s", job, r["error"])
            return []
        return [{"action": "champion", "name": f"{job} champion moves to {target['name']}", "village": "",
                 "reason": "new home", "time": int(time.time())}]

    # --- the war champion clears oases ----------------------------------------------------------------

    def clear_oasis(self, army_v, coords, offence, raid_book, plan, hall_level):
        """One oasis with animals near the army village, when the war champion and the army can win clearly.
        Oases within 3 squares come first (annexed when the Hall has room), then any within `radius`."""
        heroid = self.war_champion(army_v["id"])
        c = coords.get(army_v["id"])
        if heroid is None or not c or not offence or army_v["state"]["incoming"]["attacks"]:
            return []
        now = time.time()
        cands = []
        for key, o in raid_book.data.get("oases", {}).items():
            d = math.hypot(o["x"] - c[0], o["y"] - c[1])
            if d > plan["champion"]["oasis_radius"] or now - self.data["oases"].get(key, 0) < OASIS_COOLDOWN_S:
                continue
            near = abs(o["x"] - c[0]) <= 3 and abs(o["y"] - c[1]) <= 3
            cands.append((0 if near and hall_level >= 10 else 1, d, key, o))
        for _, d, key, o in sorted(cands)[:3]:
            animals = self.c.oasis_troops(o["x"], o["y"])
            self.data["oases"][key] = now
            if not animals:
                continue   # empty (the raiders take those) or not a free oasis any more
            inf, cav = combat.attack_power(offence)
            need = plan["counter"]["margin"] * combat.defence_power(animals, inf / max(1, inf + cav))
            force = combat.size_force(offence, need)
            if force is None:
                continue
            slots = {combat.slot_of(u): n for u, n in force.items()}
            what = f"clear the oasis ({o['x']}|{o['y']}) with the war champion and {sum(force.values())} troops"
            log.info("[%s] champion: %s%s", army_v["name"], "[dry-run] " if self.dry_run else "", what)
            if not self.dry_run:
                if army_v["id"] is not None:
                    self.c.select_village(army_v["id"])
                r = self.c.send_army(o["x"], o["y"], 4, slots, champ_id=heroid)
                if not r["ok"]:
                    log.info("champion: the game refused: %s (%s)", what, r["error"])
                    continue
            return [{"action": "raid", "name": what, "village": army_v["name"], "slot": 39, "reason": "oasis",
                     "time": int(now)}]
        return []

    def summary(self):
        return {job: {"unit": _uname(c["unit"]), "home": c["home"], "wref": c["wref"]} for job, c in self.champs.items()}


def _links(html):
    import re
    return [h.replace("&amp;", "&") for h in re.findall(r'href="([^"]*train=\d+[^"]*)"', html)]


def _uname(u):
    from . import units
    return units.name(u)
