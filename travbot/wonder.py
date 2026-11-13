"""Support for the alliance's Wonder and relics (Brandon 2026-11-13: "let the humans build the wonder and support
them. If there is a wonder village in alliance the bots should be sending resources and troops").

The game's alliance feed marks a member's village that holds the Wonder (`ww`, `ww_level`) or relics (`relics`)
- GameEngine/BotFeed.php. From that:

resources  every bot village with a Marketplace and free merchants ships what it holds above
           `wonder.keep_share` of its storage to the nearest alliance Wonder (bots never build it themselves);
troops     bot villages within `wonder.radius` move `wonder.troop_share` of their defenders above
           `defense.reinforce_keep` into the Wonder village, at most once per `wonder.troop_every_hours` per
           village; from the day the Etruscans attack Wonders the share doubles;
relics     a member's village holding a relic gets a garrison the same way (`wonder.relic_share`).
Attacks on these villages are also answered by the normal reinforcement (Bot.reinforce).
"""
import json
import logging
import math
import threading
import time

from . import rules

log = logging.getLogger("travbot")
_LOCK = threading.Lock()


def wonder_villages(feed):
    """[{"owner", "wref", "x", "y", "name", "level"}] of alliance members' Wonder villages."""
    out = []
    for m in (feed or {}).get("members", []):
        for v in m.get("villages", []):
            if v.get("ww"):
                out.append({"owner": m["name"], "wref": v["wref"], "x": v["x"], "y": v["y"], "name": v["name"],
                            "level": v.get("ww_level", 0)})
    return out


def relic_villages(feed):
    out = []
    for m in (feed or {}).get("members", []):
        for v in m.get("villages", []):
            if v.get("relics"):
                out.append({"owner": m["name"], "wref": v["wref"], "x": v["x"], "y": v["y"], "name": v["name"],
                            "relics": v["relics"]})
    return out


class Support:
    def __init__(self, client, tribe, state_dir, dry_run):
        self.c = client
        self.tribe = tribe
        self.dry_run = dry_run
        self.path = state_dir / "support.json"
        try:
            self.sent = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.sent = {}

    def _save(self):
        if not self.dry_run:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.sent), encoding="utf-8")
            tmp.replace(self.path)

    def step(self, villages, coords, plan, feed, natars_attacking):
        """villages: [{"id", "name", "state", "reserved"}]. Returns actions."""
        cfg = plan["wonder"]
        if not cfg["enabled"] or not feed:
            return []
        wonders = wonder_villages(feed)
        relics = relic_villages(feed)
        if not wonders and not relics:
            return []
        actions = []
        if wonders:
            actions += self._resources(villages, coords, cfg, wonders)
        share = cfg["troop_share"] * (2 if natars_attacking else 1)
        for t in wonders:
            actions += self._troops(villages, coords, plan, t, share, "Wonder")
        for t in relics:
            actions += self._troops(villages, coords, plan, t, cfg["relic_share"], "relic")
        self._save()
        return actions

    def _resources(self, villages, coords, cfg, wonders):
        actions = []
        for v in villages:
            st, c = v["state"], coords.get(v["id"])
            slot = rules.find_slot(st, "Marketplace")
            if not c or slot is None or st["incoming"]["attacks"]:
                continue
            w = min(wonders, key=lambda w: math.hypot(w["x"] - c[0], w["y"] - c[1]))
            if (w["x"], w["y"]) == tuple(c):
                continue
            if v["id"] is not None:
                self.c.select_village(v["id"])
            m = self.c.market(slot)
            if not m or m["merchants"] <= 0:
                continue
            state = self.c.village()
            keep = (v.get("reserved") or {}).get("cost") or {}
            ship, room = {}, m["merchants"] * m["carry"]
            for k in ("wood", "clay", "iron", "crop"):
                r = state["resources"][k]
                ship[k] = max(0, r["amount"] - max(int(cfg["keep_share"] * r["cap"]), keep.get(k, 0)))
            total = sum(ship.values())
            if total < cfg["min_shipment"]:
                continue
            if total > room:
                ship = {k: int(n * room / total) for k, n in ship.items()}
            if sum(ship.values()) < cfg["min_shipment"]:
                continue
            what = ", ".join(f"{n} {k}" for k, n in ship.items() if n)
            log.info("[%s] wonder: %ssend %s to %s's Wonder %s (%s|%s)", v["name"], "[dry-run] " if self.dry_run else "",
                     what, w["owner"], w["name"], w["x"], w["y"])
            if not self.dry_run and not self.c.send_resources(slot, w["x"], w["y"], ship):
                log.info("[%s] the game refused the shipment to the Wonder", v["name"])
                continue
            actions.append({"action": "send", "name": f"resources to {w['owner']}'s Wonder", "slot": slot,
                            "reason": what, "village": v["name"], "time": int(time.time())})
        return actions

    def _troops(self, villages, coords, plan, t, share, kind):
        cfg, d = plan["wonder"], plan["defense"]
        unit = rules.basic_defender(self.tribe)
        now, actions = time.time(), []
        for v in villages:
            c, st = coords.get(v["id"]), v["state"]
            if not c or tuple(c) == (t["x"], t["y"]) or st["incoming"]["attacks"]:
                continue
            if math.hypot(c[0] - t["x"], c[1] - t["y"]) > cfg["radius"] or not rules.find_slot(st, "Rally Point"):
                continue
            key = f"{v['id']}>{t['wref']}"
            if now - self.sent.get(key, 0) < cfg["troop_every_hours"] * 3600:
                continue
            count = int((st["troops"].get(unit, 0) - d["reinforce_keep"]) * share)
            if count < 10:
                continue
            log.info("[%s] wonder: %ssend %d defenders to %s's %s village %s (%s|%s)", v["name"],
                     "[dry-run] " if self.dry_run else "", count, t["owner"], kind, t["name"], t["x"], t["y"])
            ok = True
            if not self.dry_run:
                if v["id"] is not None:
                    self.c.select_village(v["id"])
                ok = self.c.send_army(t["x"], t["y"], 2, {1: count})["ok"]
            if not ok:
                continue
            self.sent[key] = now
            actions.append({"action": "reinforce", "name": f"{count} defenders to {t['owner']}'s {kind} village",
                            "slot": 39, "reason": kind, "village": v["name"], "time": int(now)})
        return actions
