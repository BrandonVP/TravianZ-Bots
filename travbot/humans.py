"""Help for the human members of the bot's alliance (plan 2026-10-29-ai-allies-for-players, sitting 3).

The game's feed ajax.php?f=tzbot&a=ally lists the alliance: members (bot or not), their villages and the attacks
coming in on them. From it:

gifts      a young village of an active human member gets resources from the NEAREST bot village (only that bot
           gives), at most once per member per day across all bots (shared ledger next to the state folders),
           at most `human_gift_hours` of the donor village's production, never below the donor's reserve.
           A short template note goes with a gift, at most every few days.
defence    villages of human members with an attack coming in become reinforcement targets (Bot.reinforce),
           with a template note to the member when troops are sent.

Letters are templates (diplomacy.template_letter); the bot never reads its inbox - real questions are answered by
hand (mail/mailbot.py).
"""
import json
import logging
import math
import threading
import time

from . import rules
from .diplomacy import template_letter

log = logging.getLogger(__name__)

ACTIVE_S = 3 * 24 * 3600        # members inactive longer get no gifts (abandoned villages feed raiders)
GIFT_EVERY_S = 24 * 3600        # one gift per member per day, whichever bot gives it
NOTE_EVERY_S = 3 * 24 * 3600    # a gift note at most every 3 days per member
_LOCK = threading.Lock()        # the bots run as threads of one process and share the ledger


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


class HumanAllies:
    def __init__(self, state_dir):
        self.ledger_path = state_dir.parent / "human_gifts.json"
        self.ally = None

    def refresh(self, client):
        """Read the alliance feed once per round (None when not in an alliance or the game has no feed)."""
        try:
            data = json.loads(client.get("ajax.php?f=tzbot&a=ally"))
            self.ally = data if data.get("alliance") else None
        except ValueError:
            self.ally = None
        return self.ally

    def humans(self, me):
        if not self.ally:
            return []
        return [m for m in self.ally["members"] if not m["bot"] and m["name"] != me]

    def _bot_villages(self):
        return [(v, m["name"]) for m in self.ally["members"] if m["bot"] for v in m["villages"]]

    # --- defence ----------------------------------------------------------------

    def targets(self, me):
        """Reinforcement targets: human members' villages with an attack or raid coming in."""
        if not self.ally:
            return []
        owner = {v["wref"]: m for m in self.humans(me) for v in m["villages"]}
        out = {}
        for a in self.ally["incoming"]:
            m = owner.get(a["to"])
            if not m:
                continue
            v = next(v for v in m["villages"] if v["wref"] == a["to"])
            t = out.setdefault(a["to"], {"name": f"{m['name']}'s {v['name']}", "coords": (v["x"], v["y"]),
                                         "incoming": {"attacks": 0}, "human": m["name"], "village": v["name"],
                                         "arrives": a["arrives"]})
            t["incoming"]["attacks"] += 1
            t["arrives"] = min(t["arrives"], a["arrives"])
        return list(out.values())

    def note_reinforcement(self, client, target, what, me, dry_run):
        """Tell the member help is coming (once per reinforcement; Bot.reinforce has its own cooldown)."""
        if dry_run or not target.get("human"):
            return
        subject, text = template_letter("reinforce", {"me": me, "to": target["human"], "village": target["village"],
                                                      "what": what})
        client.send_message(target["human"], subject, text)

    # --- gifts ------------------------------------------------------------------

    def gifts(self, client, villages, plan, me, coords, dry_run):
        """Resources for young villages of active human members. `villages`: this bot's villages (with state).
        Returns actions."""
        sp = plan["share"]
        if not self.ally or not sp.get("humans", True):
            return []
        now = time.time()
        bot_villages = self._bot_villages()
        mine = {tuple(coords[v["id"]]): v for v in villages if coords.get(v["id"])}
        actions = []
        for m in self.humans(me):
            if now - m["last_active"] > ACTIVE_S:
                continue
            attacked = {a["to"] for a in self.ally["incoming"]}
            young = [v for v in m["villages"] if v["pop"] < sp.get("human_young_pop", 500) and v["wref"] not in attacked]
            if not young:
                continue
            target = min(young, key=lambda v: v["pop"])
            near = min(bot_villages, key=lambda bv: math.hypot(bv[0]["x"] - target["x"], bv[0]["y"] - target["y"]), default=None)
            if not near or near[1] != me:
                continue  # the nearest bot village belongs to another bot: that bot gives
            donor = mine.get((near[0]["x"], near[0]["y"]))
            if not donor or donor["state"]["incoming"]["attacks"]:
                continue
            slot = rules.find_slot(donor["state"], "Marketplace")
            if not slot:
                continue
            with _LOCK:
                ledger = _load(self.ledger_path, {})
                rec = ledger.get(str(m["uid"]), {})
                if now - rec.get("gift", 0) < GIFT_EVERY_S:
                    continue
                if donor["id"] is not None:
                    client.select_village(donor["id"])
                market = client.market(slot)
                if not market or market["merchants"] <= 0:
                    continue
                state = client.village()
                ship = self._gift(state, sp, market["merchants"] * market["carry"], (donor.get("reserved") or {}).get("cost"))
                if not ship:
                    continue
                what = ", ".join(f"{k} {n}" for k, n in ship.items() if n)
                log.info("[%s] %sgift %s to %s's %s (%s|%s)", donor["name"], "[dry-run] " if dry_run else "",
                         what, m["name"], target["name"], target["x"], target["y"])
                if not dry_run:
                    if not client.send_resources(slot, target["x"], target["y"], ship):
                        log.info("[%s] the game refused the gift to %s", donor["name"], m["name"])
                        continue
                    rec["gift"] = now
                    if now - rec.get("note", 0) >= NOTE_EVERY_S:
                        subject, text = template_letter("gift", {"me": me, "to": m["name"], "village": target["name"],
                                                                 "what": what})
                        client.send_message(m["name"], subject, text)
                        rec["note"] = now
                    ledger[str(m["uid"])] = rec
                    _save(self.ledger_path, ledger)
                actions.append({"action": "send", "name": f"gift to {m['name']}'s {target['name']}", "slot": slot,
                                "reason": what, "village": donor["name"], "time": int(now)})
        return actions

    @staticmethod
    def _gift(state, sp, space, reserved_cost=None):
        """At most `human_gift_hours` of each resource's production, never below the donor's reserve, fitting the
        merchants. None if not worth sending."""
        hours = sp.get("human_gift_hours", 1.0)
        keep_share = max(sp["donor_keep_share"], sp.get("ally_keep_share", 0))
        want = {}
        for k in ("wood", "clay", "iron", "crop"):
            r = state["resources"][k]
            keep = max(int(keep_share * r["cap"]), (reserved_cost or {}).get(k, 0))
            want[k] = max(0, min(int(max(r["prod"], 0) * hours), r["amount"] - keep))
        total = sum(want.values())
        if total <= 0 or space <= 0:
            return None
        if total > space:
            f = space / total
            want = {k: int(v * f) for k, v in want.items()}
        if sum(want.values()) < sp.get("human_min_gift", 300):
            return None
        return want
