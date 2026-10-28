"""Raiding (plan //TRUENAS/Ai/Claude/plans/2026-10-26-bot-raiding.md): farm safe, worthwhile targets
for resources, the way a careful player does.

Targets, in order of preference, within `raid.radius` tiles of the raiding village:
  1. inactive players: a village whose population has not changed for `raid.inactive_days`
  2. empty oases: unoccupied, no animals (checked on the tile, at most once per `rescout_hours`)
  3. the admin farm accounts (`raid.farm_accounts`): only after scouting shows no defenders at home
Never: own villages, the allied bots, the own alliance, `raid.exclude` accounts (admins, Natars,
Nature), and active players (`raid.active_players` is off).

Players' villages are scouted before the first raid (farms: every `rescout_hours`); troops at home
mark the target "defended" for `block_days`. A raid with losses blocks the target for `block_days`,
one where every raider died blocks it for good. Wave size adapts to the loot: full carry -> about
50% more next time (up to `max_wave`), half empty -> fewer. A target is raided again after
`cooldown_hours`, longer when the last raid brought little.

Everything here is pure bookkeeping on `raid.json` in the bot's state folder; main.py does the I/O.
"""
import json
import math
import time

from . import units

# raider preference per tribe: the first one the village can train is used
RAIDERS = {1: [5, 3, 1], 2: [16, 13, 11], 3: [24, 22]}
SCOUTS = {1: 4, 2: 14, 3: 23}

DAY = 86400
REPORT_GRACE_S = 3 * 3600   # a send without a report after this long no longer counts as out (+ trip time)


def raider_choices(tribe):
    return RAIDERS.get(tribe, [])


def scout_unit(tribe):
    return SCOUTS.get(tribe)


def form_slot(unit_id):
    """Rally Point form slot (t1..t10) of a classic unit."""
    return (unit_id - 1) % 10 + 1


class RaidBook:
    def __init__(self, state_dir):
        self.path = state_dir / "raid.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        for k, v in (("villages", {}), ("targets", {}), ("out", []), ("seen_reports", []),
                     ("oases", {}), ("scanned", 0), ("totals", {})):
            self.data.setdefault(k, v)

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    # --- map knowledge --------------------------------------------------------

    def observe(self, tiles, now=None):
        """Record the villages and oases of a map scan (population history for inactivity)."""
        now = now or time.time()
        vs, oases = self.data["villages"], self.data["oases"]
        for t in tiles:
            if t["wref"] is None:
                continue
            key = str(t["wref"])
            if t["village"] and t["pop"] is not None:
                v = vs.get(key)
                if v is None or v.get("owner") != t["owner"]:
                    vs[key] = {"x": t["x"], "y": t["y"], "name": t["village"], "owner": t["owner"],
                               "alliance": t["alliance"], "tribe": t["tribe"], "pop": t["pop"],
                               "since": now, "seen": now}
                else:
                    if v["pop"] != t["pop"]:
                        v["pop"], v["since"] = t["pop"], now
                    v.update(seen=now, alliance=t["alliance"], name=t["village"])
            elif t["oasis"] and not t["owner"]:
                o = oases.setdefault(key, {"x": t["x"], "y": t["y"], "animals": None, "checked": 0})
                o["seen"] = now
            elif t["oasis"]:
                oases.pop(key, None)   # occupied now
        self.data["scanned"] = now

    def scan_due(self, hours=24, now=None):
        return (now or time.time()) - self.data.get("scanned", 0) > hours * 3600

    def oases_to_check(self, near, radius, rescout_hours, limit=5, now=None):
        """Unoccupied oases near (x, y) whose animal count is unknown or old, nearest first."""
        now = now or time.time()
        due = [(dist(near, (o["x"], o["y"])), k) for k, o in self.data["oases"].items()
               if dist(near, (o["x"], o["y"])) <= radius and now - o.get("checked", 0) > rescout_hours * 3600]
        return [k for _, k in sorted(due)[:limit]]

    def set_animals(self, key, n, now=None):
        o = self.data["oases"].get(key)
        if o is not None:
            o.update(animals=n, checked=now or time.time())

    # --- target choice --------------------------------------------------------

    def kind_of(self, key, cfg, me, ally_names, own_alliance, now):
        """'inactive' / 'farm' / 'oasis' / None (not a target) for a known tile."""
        if key in self.data["oases"]:
            o = self.data["oases"][key]
            return "oasis" if o.get("animals") == 0 and now - o.get("checked", 0) < cfg["rescout_hours"] * 3600 else None
        v = self.data["villages"].get(key)
        if v is None or not v.get("owner"):
            return None
        owner = v["owner"]
        if owner == me or owner in ally_names or owner in cfg["exclude"] or v.get("tribe") in (4, 5):
            return None
        if own_alliance and v.get("alliance") == own_alliance:
            return None
        if owner in cfg["farm_accounts"]:
            return "farm"
        if now - v["since"] >= cfg["inactive_days"] * DAY:
            return "inactive"
        return "active" if cfg["active_players"] else None

    def plan_raids(self, origin, cfg, me, ally_names, own_alliance, now=None):
        """Targets for the village at `origin` (x, y), best first: [{"key", "x", "y", "kind", "action"}]
        with action 'scout' (needs a look first) or 'raid'."""
        now = now or time.time()
        order = {"inactive": 0, "oasis": 1, "farm": 2, "active": 3}
        busy = {o["target"] for o in self.out(now) if o.get("report") is None}   # still on the way there
        cands = []
        for key in list(self.data["villages"]) + list(self.data["oases"]):
            kind = self.kind_of(key, cfg, me, ally_names, own_alliance, now)
            if kind is None or key in busy:
                continue
            src = self.data["villages"].get(key) or self.data["oases"][key]
            d = dist(origin, (src["x"], src["y"]))
            if d > cfg["radius"]:
                continue
            t = self.data["targets"].get(key, {})
            if t.get("blocked_until", 0) > now or now < t.get("next", 0):
                continue
            if kind == "oasis":
                action = "raid"
            else:
                scouted = t.get("scouted", 0)
                stale = kind == "farm" and now - scouted > cfg["rescout_hours"] * 3600
                if not scouted or stale:
                    action = "scout"
                elif t.get("defended"):
                    continue
                else:
                    action = "raid"
            cands.append((order[kind], d, {"key": key, "x": src["x"], "y": src["y"], "kind": kind,
                                           "action": action, "name": src.get("name") or "oasis",
                                           "owner": src.get("owner")}))
        return [c for _, _, c in sorted(cands, key=lambda c: (c[0], c[1]))]

    def wave(self, key, cfg):
        """How many raiders to send to this target now."""
        t = self.data["targets"].get(key, {})
        last, ratio = t.get("wave"), t.get("ratio")
        if not last:
            return cfg["first_wave"]
        if ratio is None:
            return last
        if ratio >= 0.9:
            return min(cfg["max_wave"], int(math.ceil(last * 1.5)))
        if ratio < 0.5:
            return max(cfg["min_wave"], int(round(last * max(0.5, ratio * 1.4))))
        return last

    # --- sending and learning -------------------------------------------------

    def out(self, now=None):
        """Raids/scouts still under way (no report yet, or report seen and the troops not back)."""
        now = now or time.time()
        return [o for o in self.data["out"] if o.get("back", now + 1) > now]

    def out_from(self, origin_id, now=None):
        return [o for o in self.out(now) if o["from"] == origin_id]

    def sent(self, origin_id, key, action, count, now=None):
        now = now or time.time()
        self.data["out"].append({"from": origin_id, "target": key, "action": action, "count": count,
                                 "sent": now, "back": now + REPORT_GRACE_S})
        t = self.data["targets"].setdefault(key, {})
        if action == "raid":
            t["wave"] = count
        t["last_sent"] = now

    def learn(self, rep, raider_ids, scout_id, cfg, now=None):
        """Use one report (parse.parse_report) of our own raid or scouting. Returns a log line or None."""
        now = now or time.time()
        key = str(rep.get("target"))
        pending = [o for o in self.data["out"] if o["target"] == key and o.get("report") is None]
        if not pending:
            return None
        o = min(pending, key=lambda o: o["sent"])
        trip = max(60, now - o["sent"])
        o.update(report=now, back=now + trip)   # back home about as long as it took to get there
        t = self.data["targets"].setdefault(key, {})
        sent = sum(rep["sent"].values())
        dead = sum(rep["dead"].values())
        if o["action"] == "scout" or set(rep["sent"]) == {scout_id}:
            t["scouted"] = now
            defended = rep["defenders"] is None or rep["defenders"] > 0 or (sent and dead >= sent)
            t["defended"] = bool(defended)
            if defended:
                t["blocked_until"] = now + cfg["block_days"] * DAY
            return f"scouted {key}: " + ("defended" if defended else "no defenders, free to raid")
        ratio = rep["loot"] / rep["carry"] if rep["carry"] else 0.0
        t.update(ratio=round(ratio, 2), last_loot=rep["loot"])
        tot = self.data["totals"].setdefault(time.strftime("%Y-%m-%d", time.localtime(now)), {"raids": 0, "loot": 0})
        tot["raids"] += 1
        tot["loot"] += rep["loot"]
        if sent and dead >= sent:
            t["blocked_until"] = now + 3650 * DAY
            t["defended"] = True
            return f"raid on {key}: all {sent} raiders lost - target blocked for good"
        if dead:
            t["blocked_until"] = now + cfg["block_days"] * DAY
            return f"raid on {key}: lost {dead}/{sent}, loot {rep['loot']}/{rep['carry']} - blocked {cfg['block_days']} days"
        # come back when the storage has refilled: sooner after a full carry
        t["next"] = now + trip + cfg["cooldown_hours"] * 3600 * (2.0 - min(1.0, ratio))
        return f"raid on {key}: loot {rep['loot']}/{rep['carry']} ({ratio:.0%})"

    def forget_old(self, now=None):
        now = now or time.time()
        self.data["out"] = [o for o in self.data["out"] if o.get("back", 0) > now - DAY]
        self.data["seen_reports"] = self.data["seen_reports"][-500:]
        days = sorted(self.data["totals"])
        for d in days[:-14]:
            del self.data["totals"][d]

    def summary(self, now=None):
        """For the status page."""
        now = now or time.time()
        tg = self.data["targets"]
        return {
            "out": len(self.out(now)),
            "known_villages": len(self.data["villages"]),
            "empty_oases": sum(1 for o in self.data["oases"].values() if o.get("animals") == 0),
            "blocked": sum(1 for t in tg.values() if t.get("blocked_until", 0) > now),
            "totals": dict(sorted(self.data["totals"].items())[-3:]),
        }


def dist(a, b):
    """Distance on the wrapping 401x401 map."""
    dx = abs(a[0] - b[0]) % 401
    dy = abs(a[1] - b[1]) % 401
    return math.hypot(min(dx, 401 - dx), min(dy, 401 - dy))


def unit_name(uid):
    return units.name(uid)
