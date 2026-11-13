"""Letters become requests (plan 2026-11-13-smarter-bots, section 4).

1. Read:  the AI model lists what a letter asks the bot to DO (resources, reinforce, scout, attack), with the
          coordinates and landing time written in it. It only reads; it cannot act.
2. Check: the rules here decide. Only alliance members get help; resources and troops go only to the sender's
          own villages; attacks only on players on the hostile list (they attacked the alliance) or on Etruscan
          (Natar) villages; daily limits per member; what the bot can spare.
3. Do:    resources, reinforcements, scouts and attacks are sent now, or at the right moment for "land at 20:00"
          (Army.schedule; the bot reads the travel time from the game's confirmation page).
4. Reply: the mailer writes the answer afterwards, from the facts returned here, so it never promises more.
Scouting asked for by a member is reported back to them when the scouts' report comes in (Army.learn).
"""
import json
import logging
import math
import re
import threading
import time
from datetime import datetime, timedelta

import requests as http

from . import combat, rules, units

log = logging.getLogger("travbot")
_LOCK = threading.Lock()
NONE = -9999
COORDS = re.compile(r"\(?\s*(-?\d{1,3})\s*[|/,]\s*(-?\d{1,3})\s*\)?")

REQ_SCHEMA = {
    "type": "object",
    "properties": {"requests": {"type": "array", "items": {
        "type": "object",
        "properties": {"type": {"type": "string", "enum": ["resources", "reinforce", "scout", "attack", "other"]},
                       "x": {"type": "integer"}, "y": {"type": "integer"},
                       "land_at": {"type": "string"},
                       "resource": {"type": "string", "enum": ["any", "wood", "clay", "iron", "crop"]}},
        "required": ["type", "x", "y", "land_at", "resource"]}}},
    "required": ["requests"],
}
READ_PROMPT = (
    "You read a letter that a player of an online strategy game wrote to an allied ruler. List each concrete thing "
    "the player asks the ruler to DO:\n"
    "- resources: send them resources\n- reinforce: send defending troops to one of their villages\n"
    "- scout: send scouts to look at a village\n- attack: attack a village\n- other: anything else concrete\n"
    f"x, y: the coordinates written in the letter for that request, as in (x|y); {NONE} if none are written.\n"
    "land_at: the time the troops should arrive if the letter gives one, as HH:MM in 24-hour time, else an empty "
    "string. resource: the resource they need most if they say, else any.\n"
    "Return an empty list when the letter asks for nothing concrete (greetings, questions, chat).")


def _land_time(text, now=None):
    """'20:00' -> unix time of the next 20:00 (server time = the bot's TZ), or None."""
    m = re.match(r"^\s*(\d{1,2}):(\d{2})\s*$", text or "")
    if not m or int(m.group(1)) > 23 or int(m.group(2)) > 59:
        return None
    now = datetime.fromtimestamp(now or time.time())
    t = now.replace(hour=int(m.group(1)), minute=int(m.group(2)), second=0, microsecond=0)
    if t <= now:
        t += timedelta(days=1)
    return t.timestamp()


class Requests:
    def __init__(self, bot, ollama_url, models):
        self.bot = bot
        self.ollama = ollama_url.rstrip("/")
        self.models = models
        self.ledger_path = bot.state_dir.parent / "human_gifts.json"

    # --- 1. read ---------------------------------------------------------------------------------

    def read(self, sender, subject, body):
        """[{"type", "x", "y", "land_at", "resource"}] the letter asks for (coordinates only if written in it)."""
        written = {(int(a), int(b)) for a, b in COORDS.findall(body or "")}
        for model in self.models:
            try:
                r = http.post(f"{self.ollama}/api/chat", timeout=300, json={
                    "model": model, "stream": False, "think": False, "format": REQ_SCHEMA, "keep_alive": "10m",
                    "options": {"temperature": 0, "num_ctx": 4096, "num_predict": 400},
                    "messages": [{"role": "system", "content": READ_PROMPT},
                                 {"role": "user", "content": f"From {sender}, subject \"{subject}\":\n{body}"}]})
                r.raise_for_status()
                out = json.loads(r.json()["message"]["content"]).get("requests") or []
                clean = []
                for q in out[:4]:
                    if not isinstance(q, dict) or q.get("type") not in REQ_SCHEMA["properties"]["requests"]["items"][
                            "properties"]["type"]["enum"]:
                        continue
                    xy = (q.get("x"), q.get("y"))
                    if xy not in written:     # the model must not invent coordinates
                        xy = (None, None)
                    clean.append({"type": q["type"], "x": xy[0], "y": xy[1], "land_at": q.get("land_at") or "",
                                  "resource": q.get("resource") or "any"})
                return clean
            except (http.RequestException, ValueError, KeyError, TypeError) as e:
                log.warning("reading a letter with %s failed: %s", model, e)
        return None   # model unavailable: the mailer tries again later

    # --- 2.-3. check and do --------------------------------------------------------------------

    def handle(self, sender, reqs):
        """Carry out what is allowed. Returns fact lines for the reply."""
        if not reqs:
            return []
        plan = self.bot.last_plan
        cfg = plan["requests"]
        feed = self.bot.humans.ally
        member = next((m for m in (feed or {}).get("members", []) if m["name"].lower() == sender.lower()), None)
        if member is None:
            return ["The writer is not a member of your alliance, so you only answer kindly; you did not act on "
                    "any request."]
        if not cfg["enabled"]:
            return ["You did not act on the requests this time."]
        facts = []
        with _LOCK:
            ledger = self._ledger()
            rec = ledger.setdefault(str(member["uid"]), {})
            day = [t for t in rec.get("requests", []) if time.time() - t < 86400]
            for q in reqs:
                if q["type"] == "other":
                    continue
                if len(day) >= cfg["per_member_per_day"]:
                    facts.append(f"You already helped {sender} {len(day)} times today; this request waits until tomorrow.")
                    break
                fn = getattr(self, "_" + q["type"])
                try:
                    fact, done = fn(member, q, plan, rec)
                except Exception as e:  # one request must not stop the others or the bot
                    log.exception("request %s from %s failed", q["type"], sender)
                    fact, done = f"Something went wrong with the {q['type']} request ({type(e).__name__}).", False
                facts.append(fact)
                if done:
                    day.append(time.time())
            rec["requests"] = day
            if not self.bot.dry_run:
                self._save(ledger)
        log.info("requests from %s: %s", sender, " | ".join(facts))
        return facts

    def _ledger(self):
        try:
            return json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _save(self, data):
        tmp = self.ledger_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
        tmp.replace(self.ledger_path)

    def _member_village(self, member, q):
        vs = member.get("villages", [])
        if q["x"] is not None:
            v = next((v for v in vs if (v["x"], v["y"]) == (q["x"], q["y"])), None)
            return v
        return min(vs, key=lambda v: v["pop"]) if vs else None

    def _nearest(self, x, y, need):
        """This bot's villages that pass `need(v)`, nearest to (x|y) first: [(distance, v, (vx, vy))]."""
        out = []
        for v in self.bot.last_seen:
            c = self.bot.coords.get(v["id"])
            if c and need(v):
                out.append((math.hypot(c[0] - x, c[1] - y), v, tuple(c)))
        return sorted(out, key=lambda t: t[0])

    def _resources(self, member, q, plan, rec):
        cfg = plan["requests"]
        target = self._member_village(member, q)
        if target is None:
            return ("They asked for resources but the coordinates are not one of their villages; you asked "
                    "them to write the coordinates of their own village.", False)
        if time.time() - rec.get("req_gift", 0) < 86400:
            return ("You already sent them resources on request today; more tomorrow.", False)
        for dist, v, c in self._nearest(target["x"], target["y"],
                                        lambda v: rules.find_slot(v["state"], "Marketplace")
                                        and not v["state"]["incoming"]["attacks"]):
            slot = rules.find_slot(v["state"], "Marketplace")
            if v["id"] is not None:
                self.bot.client.select_village(v["id"])
            market = self.bot.client.market(slot)
            if not market or market["merchants"] <= 0:
                continue
            state = self.bot.client.village()
            ship = {}
            for k in ("wood", "clay", "iron", "crop"):
                r = state["resources"][k]
                give = int(max(r["prod"], 0) * cfg["gift_hours"] * (1.5 if q["resource"] == k else 1.0))
                ship[k] = max(0, min(give, r["amount"] - int(plan["share"]["donor_keep_share"] * r["cap"])))
            room = market["merchants"] * market["carry"]
            if sum(ship.values()) > room:
                f = room / sum(ship.values())
                ship = {k: int(n * f) for k, n in ship.items()}
            if sum(ship.values()) < 300:
                continue
            what = ", ".join(f"{n} {k}" for k, n in ship.items() if n)
            if not self.bot.dry_run and not self.bot.client.send_resources(slot, target["x"], target["y"], ship):
                continue
            rec["req_gift"] = time.time()
            return (f"You sent {what} from {v['name']} to their village {target['name']} "
                    f"({target['x']}|{target['y']}); merchants are on the way.", True)
        return ("You could not spare resources right now (no free merchants or your storage is low).", False)

    def _reinforce(self, member, q, plan, rec):
        cfg, d = plan["requests"], plan["defense"]
        target = self._member_village(member, q)
        if target is None:
            return ("They asked for troops but the coordinates are not one of their villages.", False)
        unit = rules.basic_defender(self.bot.tribe)
        land = _land_time(q["land_at"])
        sent = []
        for dist, v, c in self._nearest(target["x"], target["y"],
                                        lambda v: rules.find_slot(v["state"], "Rally Point")
                                        and not v["state"]["incoming"]["attacks"]):
            if dist > d["reinforce_radius"] or (c == (target["x"], target["y"])):
                continue
            count = int((v["state"]["troops"].get(unit, 0) - d["reinforce_keep"]) * cfg["troop_share"])
            if count < 5:
                continue
            what = f"{count} {units.plural(units.name(unit))} from {v['name']} to their village {target['name']}"
            fact = self._go(v, target["x"], target["y"], 2, {1: count}, what, land)
            if fact:
                sent.append(fact)
            if len(sent) >= 2:
                break
        if not sent:
            return (f"You had no defenders to spare within {d['reinforce_radius']} squares of their village.", False)
        return (" ".join(sent), True)

    def _scout(self, member, q, plan, rec):
        if q["x"] is None:
            return ("They asked for scouting but gave no coordinates.", False)
        scout = combat.SCOUTS.get(self.bot.tribe)
        n = plan["counter"]["scouts_per_run"]
        for dist, v, c in self._nearest(q["x"], q["y"], lambda v: v["state"]["troops"].get(scout, 0) >= n
                                        and rules.find_slot(v["state"], "Rally Point")):
            what = f"{n} scouts from {v['name']} to ({q['x']}|{q['y']})"
            fact = self._go(v, q["x"], q["y"], 3, {combat.slot_of(scout): n}, what, None, spy=1)
            if fact:
                from .client import tile_id
                self.bot.army.data.setdefault("scout_requests", {})[str(tile_id(q["x"], q["y"]))] = member["name"]
                return (fact + " You will write again with what they find.", True)
        return ("You have no scouts at home right now.", False)

    def _attack(self, member, q, plan, rec):
        cfg = plan["requests"]
        if q["x"] is None:
            return ("They asked for an attack but gave no coordinates.", False)
        tile = self._map_tile(q["x"], q["y"])
        if not tile or not tile.get("owner"):
            return (f"There is no player village at ({q['x']}|{q['y']}).", False)
        hostile = {p["name"].lower() for p in self.bot.hostiles.active(plan["counter"]["hostile_days"]).values()}
        etruscan = tile.get("tribe") == 5
        if not etruscan and tile["owner"].lower() not in hostile:
            return (f"{tile['owner']} has not attacked your alliance, and you only strike those who do (or the "
                    f"Etruscans). You did not attack.", False)
        if not etruscan and not plan["counter"]["enabled"]:
            return ("Counter-strikes are switched off at the moment; you did not attack.", False)
        land = _land_time(q["land_at"])
        for dist, v, c in self._nearest(q["x"], q["y"], lambda v: self.bot.role_of.get(v["id"]) == "army"
                                        and rules.find_slot(v["state"], "Rally Point")
                                        and not v["state"]["incoming"]["attacks"]):
            st = v["state"]
            offence = {u: int(st["troops"].get(u, 0) * cfg["attack_share"]) for u in combat.offence_units(self.bot.tribe)}
            offence = {u: n for u, n in offence.items() if n > 0}
            if not offence:
                continue
            slots = {combat.slot_of(u): n for u, n in offence.items()}
            rams = int(st["troops"].get(combat.unit_id(self.bot.tribe, 7), 0) * cfg["attack_share"])
            if rams:
                slots[7] = rams
            ctar1 = None
            if etruscan:
                cats = int(st["troops"].get(combat.unit_id(self.bot.tribe, 8), 0) * cfg["attack_share"])
                if cats:
                    slots[8] = cats
                    if rules.building_level(st, "Rally Point") >= 10 and tile.get("village") not in ("Ancient Ruins",):
                        ctar1 = 27   # the Treasury: a relic can only be taken once it is gone
            n = sum(slots.values())
            what = f"attack on {tile['owner']}'s {tile.get('village') or 'village'} ({q['x']}|{q['y']}) with {n} troops from {v['name']}"
            fact = self._go(v, q["x"], q["y"], 3, slots, what, land, ctar1=ctar1)
            if fact:
                if not etruscan and not self.bot.dry_run:
                    uid = next((u for u, p in self.bot.hostiles.active(plan["counter"]["hostile_days"]).items()
                                if p["name"].lower() == tile["owner"].lower()), None)
                    self.bot.hostiles.note_strike(uid, self.bot.client.user, tile.get("wref"), offence)
                return (fact, True)
        return ("Your army is not at home or too small to help with an attack right now.", False)

    def _map_tile(self, x, y):
        for t in self.bot.client.map_block(x, y):
            if (t["x"], t["y"]) == (x, y):
                return t
        return None

    def _go(self, v, x, y, c, slots, what, land, spy=None, ctar1=None):
        """Send now, or schedule so the troops land at `land`. Returns a fact line or None."""
        client, army = self.bot.client, self.bot.army
        if v["id"] is not None:
            client.select_village(v["id"])
        if land is None:
            if self.bot.dry_run:
                return f"(dry run) You would send {what} now."
            r = client.send_army(x, y, c, slots, spy=spy, ctar1=ctar1)
            if not r["ok"]:
                log.info("request: the game refused %s: %s", what, r["error"])
                return None
            arrive = datetime.fromtimestamp(time.time() + (r["travel"] or 0)).strftime("%H:%M")
            return f"You sent {what}; they arrive at {arrive}."
        probe = client.send_army(x, y, c, slots, probe=True)
        if not probe["ok"] or not probe["travel"]:
            return None
        depart = land - probe["travel"]
        at = datetime.fromtimestamp(land).strftime("%H:%M")
        if depart < time.time() - 60:
            late = datetime.fromtimestamp(time.time() + probe["travel"]).strftime("%H:%M")
            return f"{what} cannot arrive by {at}; the earliest they could land is {late}, so you did not send them."
        army.schedule({"depart": depart, "vid": v["id"], "vname": v["name"], "x": x, "y": y, "c": c,
                       "slots": {str(k): n for k, n in slots.items()}, "what": what, "ctar1": ctar1})
        return f"You will send {what} at {datetime.fromtimestamp(depart).strftime('%H:%M')} so they land at {at}."
