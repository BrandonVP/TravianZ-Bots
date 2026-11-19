"""Support for the alliance's Wonder and relics (Brandon 2026-11-13: "let the humans build the wonder and support
them. If there is a wonder village in alliance the bots should be sending resources and troops").

The game's alliance feed marks a member's village that holds the Wonder (`ww`, `ww_level`) or relics (`relics`)
- GameEngine/BotFeed.php. From that:

resources  every bot village with a Marketplace and free merchants ships what it holds above
           `wonder.keep_share` of its storage to the nearest alliance Wonder (bots never build it themselves);
troops     bot villages within `wonder.radius` move `wonder.troop_share` of their defenders above
           `defense.reinforce_keep` into the Wonder village, at most once per `wonder.troop_every_hours` per
           village; the share doubles for a Wonder at level ETRUSCAN_LEVEL or more, or with an Etruscan attack on
           its way (the game sends Etruscan waves when a Wonder finishes level 5, 10, 15 ... 95 and 96-99 -
           GameEngine/Automation/AutomationBuildQueue.php; was: from a fixed world day, 2026-11-19);
relics     a member's village holding a relic gets a garrison the same way (`wonder.relic_share`).
letter     when a human member starts building a Wonder (level 1 or more), each bot writes them once: congratulations,
           and exactly the help that bot gives - shipments, defenders, more of them once the Etruscans attack it
           (Brandon 2026-11-19). Some minutes after the bot first sees it, and only while the bots are "awake".
Attacks on these villages are also answered by the normal reinforcement (Bot.reinforce).
"""
import json
import logging
import math
import random
import threading
import time

from . import rules

log = logging.getLogger("travbot")
_LOCK = threading.Lock()
ETRUSCANS_UID = 3      # the Etruscans' (Natars') account
ETRUSCAN_LEVEL = 5     # the first Wonder level that brings Etruscan attack waves


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

    @staticmethod
    def under_threat(wonder, feed):
        """True when the Etruscans attack this Wonder: it reached their first attack level, or their troops are
        on the way to it (the feed lists attacks coming in on members' villages, with the sender)."""
        if int(wonder.get("level") or 0) >= ETRUSCAN_LEVEL:
            return True
        return any(int(a.get("to") or 0) == int(wonder["wref"]) and int(a.get("from_owner") or 0) == ETRUSCANS_UID
                   for a in (feed or {}).get("incoming", []))

    def step(self, villages, coords, plan, feed):
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
        for t in wonders:
            share = cfg["troop_share"] * (2 if self.under_threat(t, feed) else 1)
            actions += self._troops(villages, coords, plan, t, share, "Wonder")
        for t in relics:
            actions += self._troops(villages, coords, plan, t, cfg["relic_share"], "relic")
        self._save()
        return actions

    def letters(self, villages, coords, plan, feed, writer, me):
        """Congratulate a human member who has started a Wonder, once per bot and Wonder, with the help this bot
        really gives. writer(kind, ctx) -> (subject, text). Returns actions."""
        from .mailer import AWAKE_FROM, AWAKE_TO, REPLY_MIN_S, REPLY_MAX_S
        cfg = plan["wonder"]
        if not cfg["enabled"] or not feed:
            return []
        humans = {m["name"] for m in feed.get("members", []) if not m.get("bot") and m["name"] != me}
        alliance = (feed.get("alliance") or {}).get("name") or ""
        now, actions = time.time(), []
        for w in wonder_villages(feed):
            if w["owner"] not in humans or int(w.get("level") or 0) < 1:
                continue
            key = f"letter:{w['owner']}:{w['wref']}"
            if key in self.sent:
                continue
            due = self.sent.get("due:" + key)
            if due is None:   # first sighting: write some minutes later, like a person would
                self.sent["due:" + key] = now + random.randint(REPLY_MIN_S, REPLY_MAX_S)
                self._save()
                continue
            if now < due or not AWAKE_FROM <= time.localtime(now).tm_hour < AWAKE_TO:
                continue
            ctx = self._letter_facts(villages, coords, cfg, w, me, alliance)
            if self.dry_run:
                log.info("wonder: [dry-run] would congratulate %s on the Wonder at %s (%s|%s): %s", w["owner"],
                         w["name"], w["x"], w["y"], ctx)
                continue
            subject, text = writer("wonder", ctx)
            self.c.send_message(w["owner"], subject, text)
            self.sent[key] = now
            self.sent.pop("due:" + key, None)
            self._save()
            log.info("wonder: congratulated %s on the Wonder at %s (%s|%s)", w["owner"], w["name"], w["x"], w["y"])
            actions.append({"action": "letter", "name": f"congratulated {w['owner']} on their Wonder", "slot": 0,
                            "reason": "Wonder", "village": "", "time": int(now)})
        return actions

    @staticmethod
    def _letter_facts(villages, coords, cfg, w, me, alliance):
        """What this bot will do for the Wonder, from its own villages (the same rules as _resources / _troops)."""
        markets, near, nearest = 0, 0, None
        for v in villages:
            c, st = coords.get(v["id"]), v["state"]
            if not c or tuple(c) == (w["x"], w["y"]):
                continue
            d = math.hypot(c[0] - w["x"], c[1] - w["y"])
            if rules.find_slot(st, "Marketplace") is not None:
                markets += 1
            if d <= cfg["radius"] and rules.find_slot(st, "Rally Point") is not None:
                near += 1
            if nearest is None or d < nearest[0]:
                nearest = (d, v["name"], c[0], c[1])
        place = None
        if nearest:
            n = max(1, round(nearest[0]))
            place = {"village": nearest[1], "x": nearest[2], "y": nearest[3],
                     "away": "one square" if n == 1 else f"about {n} squares"}
        return {"me": me, "to": w["owner"], "village": w["name"], "x": w["x"], "y": w["y"], "level": w["level"],
                "alliance": alliance, "markets": markets, "troop_villages": near, "radius": cfg["radius"],
                "nearest": place}

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
