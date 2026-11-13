"""Who attacks the alliance: the hostile list (plan 2026-11-13-smarter-bots, section 2; Brandon: "you want an ally
that has your back").

Sources:
- the game's alliance feed (ajax.php?f=tzbot&a=ally, read every round by humans.py): every attack and raid on
  a member's village, the bots' own included, with the attacker's uid, name, village and coordinates;
- the bots' own defence reports: what the attacker sent (so a counter-strike can be sized to it).

A player stays hostile for `counter.hostile_days` after their last attack. Never hostile: the Etruscans (uid 3,
handled as Natar targets), Nature, the Village of the Elders (uid 0), members of the own alliance and the bots.
The list is shared by all bot threads (hostiles.json next to the state folders), so an attack on one bot or
member is answered by whichever bot is best placed.
"""
import json
import threading
import time

_LOCK = threading.Lock()
NEVER = {0, 1, 2, 3, 4, 5}   # Village of the Elders, admin accounts, Nature, Etruscans, Taskmaster, Multihunter


def _load(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"players": {}, "moves": {}, "strikes": []}


def _save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
    tmp.replace(path)


class Hostiles:
    def __init__(self, state_dir):
        self.path = state_dir.parent / "hostiles.json"

    def observe(self, feed, now=None):
        """Record the attacks in an alliance feed. Returns [(attacker name, target owner name)] seen for the first time."""
        if not feed or not feed.get("alliance"):
            return []
        now = now or time.time()
        members = {m["uid"]: m for m in feed.get("members", [])}
        new = []
        with _LOCK:
            data = _load(self.path)
            for inc in feed.get("incoming", []):
                uid = inc.get("from_owner")
                if not uid or uid in NEVER or uid in members:
                    continue
                key = str(inc.get("moveid"))
                if key in data["moves"]:
                    continue
                data["moves"][key] = inc.get("arrives") or now
                p = data["players"].setdefault(str(uid), {"name": inc.get("from_name") or f"player {uid}",
                                                          "villages": {}, "attacks": 0, "first": now,
                                                          "victims": {}, "force": None})
                p["name"] = inc.get("from_name") or p["name"]
                p["villages"][str(inc.get("from"))] = [inc.get("from_x"), inc.get("from_y")]
                p["attacks"] += 1
                p["last"] = now
                p["last_kind"] = inc.get("kind")
                victim = members.get(inc.get("to_owner"), {}).get("name", "?")
                p["victims"][victim] = p["victims"].get(victim, 0) + 1
                new.append((p["name"], victim))
            # forget movements that have landed long ago
            data["moves"] = {k: t for k, t in data["moves"].items() if now - (t or now) < 7 * 86400}
            _save(self.path, data)
        return new

    def note_force(self, uid, troops, now=None):
        """What an attacker sent (from a defence report), so strikes can be sized to it."""
        if not uid or uid in NEVER or not troops:
            return
        with _LOCK:
            data = _load(self.path)
            p = data["players"].get(str(uid))
            if p is not None:
                p["force"] = {str(u): n for u, n in troops.items()}
                p["force_at"] = now or time.time()
                _save(self.path, data)

    def active(self, days, now=None):
        """{uid: record} of players who attacked within `days`."""
        now = now or time.time()
        data = _load(self.path)
        return {int(u): p for u, p in data["players"].items() if now - p.get("last", 0) <= days * 86400}

    def strikes_since(self, since, uid=None, bot=None):
        data = _load(self.path)
        return [s for s in data["strikes"] if s["time"] >= since and (uid is None or s["uid"] == uid)
                and (bot is None or s["bot"] == bot)]

    def note_strike(self, uid, bot, target, troops, now=None):
        with _LOCK:
            data = _load(self.path)
            now = now or time.time()
            data["strikes"].append({"uid": uid, "bot": bot, "target": target, "time": now,
                                    "troops": {str(u): n for u, n in troops.items()}})
            data["strikes"] = [s for s in data["strikes"] if now - s["time"] < 14 * 86400]
            _save(self.path, data)

    def summary(self, days, now=None):
        """Short lines for the advisor and the letters: who attacked whom recently."""
        out = []
        for uid, p in sorted(self.active(days, now).items(), key=lambda kv: -kv[1].get("last", 0)):
            victims = ", ".join(f"{v} x{n}" for v, n in p["victims"].items())
            out.append(f"{p['name']} (uid {uid}): {p['attacks']} attack(s) on {victims}")
        return out[:10]
