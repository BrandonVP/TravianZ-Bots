"""The Sentry: an AI sitter that moves players' armies out of the way of attacks while they sleep.

Game side: GameEngine/Sentry.php (plan \\TRUENAS\\Ai\\Claude\\plans\\2026-11-01-ai-sitter.md). A player with
Patronage switches the Sentry on and picks an 8-hour window; the game then lists the account in
ajax.php?f=sentry&a=watch. For each listed account this loop logs in AS THE PLAYER'S SITTER (player name + the
Sentry's password - never the player's own password) and:

  1. notes every hostile attack/raid the first time it shows up, with a random human-like reaction delay
     (REACT_MIN_S..REACT_MAX_S); an attack that lands inside that delay is not dodged,
  2. once the delay has passed, sends every unit at home (champion included) as a reinforcement to the nearest
     safe village from a=status (own villages first, then alliance members'),
  2b. sends what the free merchants can carry to the nearest safe own village (resources out of raiders' reach),
  3. after the attack has landed, sends exactly those units back home (send back, c=8),
  4. writes each step to the player's Sentry log (a=log).

The server enforces the limits (reinforce only, only inside the window, only own/alliance villages); this module
never builds, trains, raids, trades or spends gold. It runs as one extra thread next to the normal bots, and only
when <sentry dir>/sentry.json exists: {"key": TZ_SENTRY_KEY, "password": TZ_SENTRY_PASSWORD} (+ optional
"base_url"). Its state (attacks seen, armies away) is kept in <sentry dir>/state.json.
"""
import argparse
import json
import logging
import math
import os
import pathlib
import random
import time

import requests

from .client import GameClient, GameError

log = logging.getLogger("sentry")

POLL_S = 60               # how often the watch list and the watched accounts are checked
REACT_MIN_S = 5 * 60      # human-like reaction time after an attack first shows up
REACT_MAX_S = 20 * 60
LEAVE_MARGIN_S = 30       # don't start a dodge this close to the arrival
RETURN_AFTER_S = 90       # send the army home this long after the attack has landed
SAFE_GAP_S = 10 * 60      # a target village counts as unsafe if an attack lands there within this gap of ours
FORGET_S = 3 * 86400      # drop finished entries from state.json after this long
MIN_HIDE = 500            # not worth a merchant trip below this many resources


def _distance(a, b):
    return math.hypot(a["x"] - b["x"], a["y"] - b["y"])


def _mins(seconds):
    m = max(1, round(seconds / 60))
    return f"{m} minute{'s' if m != 1 else ''}"


class SentryClient(GameClient):
    """A GameClient logged in as the Sentry sitting one player's account, plus the Sentry's JSON calls."""

    def __init__(self, base_url, username, password):
        super().__init__(base_url, username, password, request_gap=1.0)
        self.session.headers["User-Agent"] = "EmpiresOfRome-Sentry/1.0"
        self.logged_in = False

    def _json(self, method, path, data=None, headers=None):
        for attempt in (1, 2):
            if not self.logged_in:
                self.login()
                self.logged_in = True
            self._pace()
            r = self.session.request(method, self.base + path, data=data, headers=headers, timeout=self.timeout)
            if r.status_code == 403 and attempt == 1:   # session expired (or the Sentry was stood down)
                self.logged_in = False
                continue
            r.raise_for_status()
            return r.json()
        return {"ok": 0}

    def status(self):
        return self._json("GET", "ajax.php?f=sentry&a=status")

    def note(self, action, detail):
        """One line in the player's Sentry log."""
        try:
            self._json("POST", "ajax.php?f=sentry&a=log", {"action": action, "detail": detail}, {"X-TZ-Bot": "1"})
        except (requests.RequestException, GameError, ValueError):
            log.exception("could not write the Sentry log for %s", self.user)

    def send_back(self, home, enforce_id, units):
        """Send stationed troops home (rally point 'send back'): the current village must be their home."""
        self.select_village(home)
        data = {"ckey": enforce_id, "id": 39, "a": 533374, "c": 8, "s1": "ok"}
        for slot, n in units.items():
            data[f"t{slot}"] = int(n)
        self.post("a2b.php", data)


class Sentry:
    def __init__(self, cfg_dir, base_url=None, dry_run=False):
        self.dir = pathlib.Path(cfg_dir)
        cfg = json.loads((self.dir / "sentry.json").read_text(encoding="utf-8"))
        self.key = cfg["key"]
        self.password = cfg["password"]
        self.base = (base_url or cfg.get("base_url") or os.environ.get("TRAVBOT_BASE_URL", "http://192.168.68.205")).rstrip("/") + "/"
        self.dry_run = dry_run
        self.clients = {}
        self.state_path = self.dir / "state.json"
        try:
            self.state = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.state = {}
        self.state.setdefault("seen", {})    # "<uid>:<moveid>" -> attack noticed, its delay and outcome
        self.state.setdefault("away", [])    # armies the Sentry moved out and has to bring home

    def save(self):
        tmp = self.state_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, indent=1), encoding="utf-8")
        tmp.replace(self.state_path)

    def watch_list(self):
        r = requests.get(self.base + "ajax.php?f=sentry&a=watch", headers={"X-TZ-Sentry-Key": self.key}, timeout=30)
        r.raise_for_status()
        return r.json().get("watch", [])

    def client(self, username):
        if username not in self.clients:
            self.clients[username] = SentryClient(self.base, username, self.password)
        return self.clients[username]

    # --- one round ------------------------------------------------------------

    def round(self):
        watch = {w["username"]: w for w in self.watch_list()}
        # accounts with an army still away are visited too, even after their window has closed
        names = set(watch) | {a["username"] for a in self.state["away"]}
        for name in sorted(names):
            try:
                self.guard(self.client(name), name in watch)
            except (requests.RequestException, GameError, ValueError, KeyError) as e:
                log.warning("%s: %s", name, e)
        self.forget()
        if not self.dry_run:
            self.save()

    def guard(self, c, watched):
        st = c.status()
        if not st.get("ok"):
            log.info("%s: no Sentry access (stood down?)", c.user)
            return
        now = st["now"]
        villages = {v["wref"]: v for v in st["villages"]}
        uid_tag = c.user
        if watched and st.get("active"):
            self.notice(c, uid_tag, villages, now)
            self.dodge(c, uid_tag, st, villages, now)
        self.bring_home(c, uid_tag, st, villages, now)

    def notice(self, c, tag, villages, now):
        """First sighting of each attack: pick the reaction delay, or note that it lands too soon."""
        for v in villages.values():
            for inc in v["incoming"]:
                key = f"{tag}:{inc['id']}"
                if key in self.state["seen"]:
                    continue
                delay = random.randint(REACT_MIN_S, REACT_MAX_S)
                entry = {"user": tag, "village": v["wref"], "move": inc["id"], "arrival": inc["arrival"], "type": inc["type"],
                         "seen": now, "react_at": now + delay, "done": None}
                if inc["arrival"] - now < delay + LEAVE_MARGIN_S:
                    entry["done"] = "too soon"
                    c.note("seen", f"A {inc['type']} on {v['name']} lands in {_mins(inc['arrival'] - now)} - "
                                   f"too soon to move the army out.")
                log.info("%s: %s on %s in %ds (react in %ds)%s", tag, inc["type"], v["name"], inc["arrival"] - now,
                         delay, " - too soon" if entry["done"] else "")
                self.state["seen"][key] = entry

    def _safe_target(self, st, villages, home, arrival):
        """Nearest target from a=status that isn't `home` and gets no attack while our army would be parked
        there (until shortly after the attack on home has landed). Alliance villages: their attacks are not
        visible to a sitter, so they count as safe."""
        def unsafe(t):
            v = villages.get(t["wref"])
            return v is not None and any(i["arrival"] <= arrival + RETURN_AFTER_S + SAFE_GAP_S for i in v["incoming"])
        src = villages[home]
        cands = [t for t in st["targets"] if t["wref"] != home and not unsafe(t)]
        own = sorted((t for t in cands if t["own"]), key=lambda t: _distance(src, t))
        ally = sorted((t for t in cands if not t["own"]), key=lambda t: _distance(src, t))
        return (own + ally or [None])[0]

    def dodge(self, c, tag, st, villages, now):
        for key, e in self.state["seen"].items():
            if e["user"] != tag or e["done"] or now < e["react_at"]:
                continue
            v = villages.get(e["village"])
            if v is None or e["arrival"] <= now + LEAVE_MARGIN_S:
                e["done"] = "missed"
                continue
            if not any(i["id"] == e["move"] for i in v["incoming"]):
                e["done"] = "gone"   # recalled by the attacker, or already handled
                continue
            troops = self._move_troops(c, tag, st, villages, v, e, now)
            hidden = self._hide_resources(c, tag, st, villages, v, e)
            e["done"] = troops if troops != "nothing home" or not hidden else "hid resources"

    def _move_troops(self, c, tag, st, villages, v, e, now):
        units = {int(k): n for k, n in v["units"].items() if n > 0}
        if not units:
            return "nothing home"
        target = self._safe_target(st, villages, v["wref"], e["arrival"])
        if target is None:
            c.note("stay", f"A {e['type']} on {v['name']} is coming, but there is no safe village to move "
                           f"the army to. It stays at home.")
            return "nowhere to go"
        total = sum(units.values())
        if self.dry_run:
            log.info("[dry run] %s: would move %d troops %s -> %s", tag, total, v["name"], target["name"])
            return "dry run"
        c.select_village(v["wref"])
        if not c.send_troops(target["x"], target["y"], 2, units):
            log.warning("%s: the game refused the move %s -> %s", tag, v["name"], target["name"])
            return "refused"
        self.state["away"].append({"username": tag, "home": v["wref"], "home_name": v["name"], "at": target["wref"],
                                   "at_name": target["name"], "units": units, "return_at": e["arrival"] + RETURN_AFTER_S})
        c.note("dodge", f"A {e['type']} on {v['name']} lands in {_mins(e['arrival'] - now)}: moved {total} troops "
                        f"to {target['name']} ({target['x']}|{target['y']}). They come home after the attack.")
        log.info("%s: moved %d troops %s -> %s", tag, total, v["name"], target["name"])
        return "moved"

    def _hide_resources(self, c, tag, st, villages, v, e):
        """Send what the free merchants can carry to the nearest safe OWN village (the game refuses anyone
        else's). Split in proportion to what the village holds. True if a shipment left."""
        res = {k: int(n) for k, n in (v.get("res") or {}).items()}
        if not v.get("market") or sum(res.values()) < MIN_HIDE:
            return False
        own = [t for t in st["targets"] if t["own"]]
        target = self._safe_target(dict(st, targets=own), villages, v["wref"], e["arrival"])
        if target is None:
            return False
        if self.dry_run:
            log.info("[dry run] %s: would hide resources %s -> %s", tag, v["name"], target["name"])
            return True
        c.select_village(v["wref"])
        m = c.market(v["market"])
        if not m or not m["merchants"] or not m["carry"]:
            return False
        cap = m["merchants"] * m["carry"]
        total = sum(res.values())
        share = min(1.0, cap / total)
        amounts = {k: int(n * share) for k, n in res.items()}
        sent = sum(amounts.values())
        if sent < MIN_HIDE or not c.send_resources(v["market"], target["x"], target["y"], amounts):
            log.warning("%s: could not hide resources %s -> %s", tag, v["name"], target["name"])
            return False
        c.note("hide", f"Sent {sent:,} resources from {v['name']} to {target['name']} with {m['merchants']} "
                       f"merchant{'s' if m['merchants'] != 1 else ''}, out of the raiders' reach.")
        log.info("%s: hid %d resources %s -> %s", tag, sent, v["name"], target["name"])
        return True

    def bring_home(self, c, tag, st, villages, now):
        keep = []
        for a in self.state["away"]:
            if a["username"] != tag:
                keep.append(a)
                continue
            home = villages.get(a["home"])
            # another attack about to land at home: wait. One whose time has passed (not resolved yet) is no
            # danger to troops on their way back - they arrive minutes later.
            danger = home and any(now <= i["arrival"] <= now + SAFE_GAP_S for i in home["incoming"])
            if now < a["return_at"] or danger:
                keep.append(a)
                continue
            row = next((r for r in st["away"] if r["home"] == a["home"] and r["at"] == a["at"]), None)
            if row is None:
                c.note("home", f"The army from {a['home_name']} is no longer at {a['at_name']} - nothing to bring home.")
                continue
            # only what the Sentry moved, and only what is still there
            units = {s: min(n, row["units"].get(str(s), row["units"].get(s, 0))) for s, n in
                     ((int(k), v) for k, v in a["units"].items())}
            units = {s: n for s, n in units.items() if n > 0}
            if units and not self.dry_run:
                c.send_back(a["home"], row["id"], units)
                c.note("home", f"The attack has passed: {sum(units.values())} troops are on their way from "
                               f"{a['at_name']} back to {a['home_name']}.")
                log.info("%s: sent %d troops home %s -> %s", tag, sum(units.values()), a["at_name"], a["home_name"])
        self.state["away"] = keep

    def forget(self):
        now = time.time()
        self.state["seen"] = {k: e for k, e in self.state["seen"].items()
                              if not e["done"] or now - e["arrival"] < FORGET_S}

    def run(self, once=False):
        log.info("Sentry on duty at %s", self.base)
        while True:
            try:
                self.round()
            except (requests.RequestException, ValueError) as e:
                log.warning("round failed: %s", e)
            except Exception:  # the Sentry must never stop (nor stop the bots next to it)
                log.exception("round crashed")
            if once:
                return
            time.sleep(POLL_S)


def main():
    ap = argparse.ArgumentParser(description="Run the Sentry on its own (testing).")
    ap.add_argument("--dir", default=os.environ.get("TRAVBOT_SENTRY_DIR", "sentry"), help="folder with sentry.json")
    ap.add_argument("--base", help="game base URL (default: sentry.json base_url or TRAVBOT_BASE_URL)")
    ap.add_argument("--once", action="store_true", help="one round, then exit")
    ap.add_argument("--dry-run", action="store_true", help="decide but don't move troops")
    ap.add_argument("--poll", type=int, help="seconds between rounds (testing)")
    ap.add_argument("--react", help="reaction delay range in seconds, e.g. 10,30 (testing)")
    args = ap.parse_args()
    global POLL_S, REACT_MIN_S, REACT_MAX_S
    if args.poll:
        POLL_S = args.poll
    if args.react:
        REACT_MIN_S, REACT_MAX_S = (int(x) for x in args.react.split(","))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    Sentry(args.dir, args.base, args.dry_run).run(once=args.once)


if __name__ == "__main__":
    main()
