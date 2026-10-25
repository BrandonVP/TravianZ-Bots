"""Bot entry point: python -m travbot.main [--once] [--dry-run] [--advise-now]

Environment:
  TRAVBOT_BASE_URL            game URL (default http://192.168.68.205)
  TRAVBOT_STATE_DIR           folder with bot-account.json, plan.json, status.json, bot.log (default ./state);
                              every sibling folder state-* with a bot-account.json is another bot account,
                              run in its own thread (its own plan, logs and status page /b/<name>)
  TRAVBOT_PLAN_MAX_AGE_HOURS  plan.json older than this is ignored (default 6)
  TRAVBOT_MAX_ACTIONS         build orders per village per tick at most (default 6)
  TRAVBOT_OLLAMA_URL          Ollama for the advisor (default http://192.168.68.87:11434; empty disables)
  TRAVBOT_IDLE_URL            idle check (default http://192.168.68.87:8911/idle.json)
  TRAVBOT_ADVISOR_MODELS      comma-separated, tried in order (default gemma4:26b,ornith:9b)
  TRAVBOT_ADVISOR_INTERVAL_MIN  minutes between successful reviews (default 60)
  TRAVBOT_ADVISOR_RETRY_MIN     minutes between attempts while the PC is busy (default 10)
  TRAVBOT_WEB_PORT            status page port inside the container (default 8080; 0 disables)
"""
import argparse
import json
import logging
import logging.handlers
import math
import os
import pathlib
import random
import threading
import time

import requests

from .names import display
from . import plan as planmod
from . import rules, units, web
from .advisor import Advisor
from .client import GameClient, GameError, tile_id
from .diplomacy import Diplomacy, template_letter
from .expand import Expansion

log = logging.getLogger("travbot")

MIN_SLEEP, MAX_SLEEP, IDLE_SLEEP, ERROR_SLEEP = 60, 15 * 60, 10 * 60, 120
ATTACK_SLEEP = 120  # check more often while an attack is incoming
REINFORCE_COOLDOWN_S = 3600  # one round of help per attacked village per hour
ALLY_STATUS_MAX_AGE_S = 2 * 3600  # ignore an allied bot's status.json older than this


FMT = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")


def setup_logging():
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    console = logging.StreamHandler()
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s [%(threadName)s] %(name)s: %(message)s"))
    root.addHandler(console)
    logging.getLogger("urllib3").setLevel(logging.WARNING)


def add_bot_log(state_dir, thread_name, also_main=False):
    """bot.log in the bot's own state folder, with only that bot's lines (each bot runs in its own thread)."""
    logfile = logging.handlers.RotatingFileHandler(state_dir / "bot.log", maxBytes=1_000_000,
                                                   backupCount=3, encoding="utf-8")
    logfile.setFormatter(FMT)
    names = {thread_name, "MainThread"} if also_main else {thread_name}
    logfile.addFilter(lambda record: record.threadName in names)
    logging.getLogger().addHandler(logfile)


def discover_state_dirs(primary):
    """The primary state folder plus every sibling state-* folder that holds a bot account."""
    extra = sorted(d for d in primary.parent.glob("state-*") if (d / "bot-account.json").is_file())
    return [primary] + extra


def ally_status(state_dir):
    """status.json of the other bots in the same alliance (their state folders sit next to this one)."""
    def alliance(d):
        try:
            return json.loads((d / "diplomacy_state.json").read_text(encoding="utf-8")).get("alliance")
        except (OSError, ValueError, AttributeError):
            return None
    mine = alliance(state_dir)
    if not mine:
        return []
    out = []
    for d in sorted(state_dir.parent.glob("state*")):
        if d.resolve() == state_dir.resolve() or not (d / "bot-account.json").is_file() or alliance(d) != mine:
            continue
        try:
            st = json.loads((d / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if time.time() - st.get("updated", 0) <= ALLY_STATUS_MAX_AGE_S:
            out.append(st)
    return out


def write_json(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    os.replace(tmp, path)


class Bot:
    def __init__(self, client, tribe, state_dir, max_actions, dry_run, diplomacy=None):
        self.client = client
        self.tribe = tribe
        self.max_actions = max_actions
        self.dry_run = dry_run
        self.diplomacy = diplomacy
        self.expansion = Expansion(state_dir)
        self.state_dir = state_dir
        self.allies = []  # status of the allied bots, refreshed every tick
        self.coords = {}  # village id -> (x, y)
        self.vname = ""   # village being handled, for log lines
        self.capital = None        # capital village id (keeps the bigger gold reserve)
        self.capital_checked = 0.0

    def _act(self, cand, href, actions, label):
        from .names import display, display_text
        log.info("[%s] %s%s %s (slot %d): %s", self.vname, label, cand["action"], display(cand["name"]),
                 cand["slot"], display_text(cand["reason"]))
        if not self.dry_run:
            self.client.follow(href)
        actions.append({**cand, "name": display(cand["name"]), "reason": display_text(cand["reason"]), "time": int(time.time())})

    def _inspect(self, cand):
        if cand["action"] == "upgrade":
            return self.client.inspect_upgrade(cand["slot"])
        return self.client.inspect_construct(cand["slot"], cand["name"]) or {"href": None, "cost": None}

    def _reservation(self, state, plan):
        """Pick the item to save up for: the top field or building candidate that is
        affordable within save_up_hours but not yet. Returns dict or None."""
        horizon = plan["save_up_hours"] * 3600
        best = None
        for pick in (rules.field_candidates, rules.building_candidates):
            cands = pick(state, plan)
            if not cands:
                continue
            info = self._inspect(cands[0])
            if info.get("href") or not info.get("cost"):
                continue  # affordable now, or blocked for another reason
            eta = rules.seconds_until_affordable(info["cost"], state["resources"])
            if 0 < eta <= horizon and (best is None or eta < best["eta"]):
                best = {"name": cands[0]["name"], "slot": cands[0]["slot"], "cost": info["cost"], "eta": eta}
        return best

    def _defend(self, state, plan, reserved, actions):
        """Train basic defenders up to the plan's target (more aggressively under attack)."""
        d = plan["defense"]
        slot = rules.find_slot(state, "Barracks")
        if slot is None or d["unit_target"] <= 0:
            return
        under_attack = state["incoming"]["attacks"] > 0
        unit = rules.basic_defender(self.tribe)
        page = self.client.train_page(slot)
        info = page["units"].get(unit)
        if info is None:
            return
        share = d["emergency_share"] if under_attack else d["budget_share"]
        count = rules.troop_order(info["cost"], state["troops"].get(unit, 0), page["training"].get(unit, 0),
                                  d["unit_target"], state["resources"], share)
        count = min(count, info["max"])
        if count <= 0:
            return
        spend = {k: c * count for k, c in info["cost"].items()}
        if reserved and not under_attack and rules.delays_reserved(reserved["cost"], state["resources"], spend):
            return
        log.info("[%s] %strain %d %s in Barracks (slot %d)%s", self.vname, "[dry-run] " if self.dry_run else "",
                 count, units.name(unit), slot, " - ATTACK INCOMING" if under_attack else "")
        if not self.dry_run:
            self.client.train(slot, unit, count)
        actions.append({"action": "train", "name": units.name(unit), "count": count, "slot": slot,
                        "reason": "attack incoming" if under_attack else f"target {d['unit_target']}",
                        "time": int(time.time())})

    def _spend_gold(self, vid, state, plan, actions):
        """Finish long-running jobs with spare village gold. Returns True if something was finished."""
        g = plan["gold"]
        reserve = g["capital_reserve"] if vid is not None and vid == self.capital else g["reserve"]
        done = False
        for job in rules.finish_choices(state["queue"], state.get("gold"), reserve, g):
            log.info("[%s] %sfinish %s -> level %s now for %s gold (%d min left, gold %d, keeping %d)",
                     self.vname, "[dry-run] " if self.dry_run else "", display(job["name"]), job["to_level"],
                     job.get("finish_price") or 50, job["seconds_left"] // 60, state["gold"]["amount"], reserve)
            if not self.dry_run:
                self.client.finish_job(job["finish_href"])
            actions.append({"action": "finish", "name": display(job["name"]), "slot": job["slot"],
                            "reason": f"{job['seconds_left'] // 60} min left, {job.get('finish_price') or 50} gold",
                            "time": int(time.time())})
            done = True
        return done

    def share(self, villages, plan):
        """Ship surplus from the richest villages (with a Marketplace) to young ones. Returns actions."""
        sp = plan["share"]
        if not sp["enabled"]:
            return []
        richest = max(rules.total_production(v["state"]) for v in villages)
        young = [v for v in villages if rules.is_young(v["state"], richest, sp) and v["x"] is not None]
        donors = sorted((v for v in villages if v not in young and rules.find_slot(v["state"], "Marketplace")),
                        key=lambda v: -rules.total_production(v["state"]))
        if sp["allies"]:  # young villages of the allied bots (not while they are being attacked)
            for a in self.allies:
                for av in a.get("villages", []):
                    st = {"resources": av["resources"]}
                    if av.get("coords") and not (av.get("incoming") or {}).get("attacks") \
                            and rules.is_young(st, richest, sp):
                        x, y = av["coords"]
                        young.append({"id": tile_id(x, y), "name": f"{a.get('account')}'s {av['name']}",
                                      "x": x, "y": y, "state": st, "ally": True})
        actions = []
        for r in young:
            rsp = {**sp, "donor_keep_share": max(sp["donor_keep_share"], sp["ally_keep_share"])} if r.get("ally") else sp
            wref = r["id"]
            # what the young village will hold once this tick's shipments arrive (the next donor tops up the rest)
            r = {**r, "state": {**r["state"], "resources": {k: dict(v) for k, v in r["state"]["resources"].items()}}}
            for d in donors:
                self.vname = d["name"]
                slot = rules.find_slot(d["state"], "Marketplace")
                if d["id"] is not None:
                    self.client.select_village(d["id"])
                m = self.client.market(slot)
                if not m or m["merchants"] <= 0 or wref in m["sending_to"]:
                    continue  # no free merchants, or a shipment to this village is already on its way
                state = self.client.village()
                ship = rules.shipment(state, r["state"], m["merchants"], m["carry"], rsp,
                                      (d.get("reserved") or {}).get("cost"))
                if not ship:
                    continue
                log.info("[%s] %ssend %s to %s (%s|%s): young %s", d["name"],
                         "[dry-run] " if self.dry_run else "",
                         ", ".join(f"{k} {n}" for k, n in ship.items() if n), r["name"], r["x"], r["y"],
                         "allied village" if r.get("ally") else "village")
                ok = True if self.dry_run else self.client.send_resources(slot, r["x"], r["y"], ship)
                if not ok:
                    log.info("[%s] the game refused the shipment to %s", d["name"], r["name"])
                    continue
                actions.append({"action": "send", "name": f"resources to {r['name']}", "slot": slot,
                                "reason": ", ".join(f"{k} {n}" for k, n in ship.items() if n),
                                "village": d["name"], "time": int(time.time())})
                for k, n in ship.items():
                    r["state"]["resources"][k]["amount"] += n
        return actions

    def _ally_tiles(self):
        """(x, y) of the allied bots' villages and of the valleys they are settling."""
        out = []
        for a in self.allies:
            out += [tuple(v["coords"]) for v in a.get("villages", []) if v.get("coords")]
            out += [(s["sent"]["x"], s["sent"]["y"]) for s in (a.get("expansion") or {}).values()
                    if isinstance(s, dict) and s.get("sent")]
        return out

    def reinforce(self, villages, plan):
        """Send defenders to own or allied villages with incoming attacks (reinforcement). Returns actions."""
        d = plan["defense"]
        if not d["reinforce"] or d["reinforce_share"] <= 0:
            return []
        targets = [{"name": v["name"], "coords": self.coords.get(v["id"]), "incoming": v["state"]["incoming"]}
                   for v in villages]
        for a in self.allies:
            targets += [{"name": f"{a.get('account')}'s {v['name']}", "coords": v.get("coords"),
                         "incoming": v.get("incoming") or {}} for v in a.get("villages", [])]
        targets = [t for t in targets if t["coords"] and (t["incoming"].get("attacks") or 0) > 0]
        if not targets:
            return []
        path = self.state_dir / "reinforce.json"
        try:
            sent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sent = {}
        unit = rules.basic_defender(self.tribe)
        now, actions = time.time(), []
        for t in targets:
            tx, ty = t["coords"]
            key = f"{tx}|{ty}"
            if now - sent.get(key, 0) < REINFORCE_COOLDOWN_S:
                continue
            for v in villages:
                c = self.coords.get(v["id"])
                if not c or tuple(c) == (tx, ty) or v["state"]["incoming"]["attacks"]:
                    continue  # unknown position, the target itself, or under attack itself
                if math.hypot(c[0] - tx, c[1] - ty) > d["reinforce_radius"] \
                        or not rules.find_slot(v["state"], "Rally Point"):
                    continue
                count = int((v["state"]["troops"].get(unit, 0) - d["reinforce_keep"]) * d["reinforce_share"])
                if count < 1:
                    continue
                self.vname = v["name"]
                log.info("[%s] %sreinforce %s (%s|%s) with %d %s: attack incoming", v["name"],
                         "[dry-run] " if self.dry_run else "", t["name"], tx, ty, count, units.name(unit))
                ok = True
                if not self.dry_run:
                    if v["id"] is not None:
                        self.client.select_village(v["id"])
                    ok = self.client.send_reinforcement(tx, ty, 1, count, unit)
                if not ok:
                    log.info("[%s] the game refused the reinforcement to %s", v["name"], t["name"])
                    continue
                sent[key] = now
                actions.append({"action": "reinforce", "name": f"{count} {units.name(unit)} to {t['name']}",
                                "slot": 39, "reason": "attack incoming", "village": v["name"], "time": int(now)})
        if not self.dry_run:
            write_json(path, {k: ts for k, ts in sent.items() if now - ts < 24 * 3600})
        return actions

    def village_tick(self, vid, state, plan, village_count):
        """Place orders for one village. Returns (state, actions, waits, reserved, note)."""
        actions, waits, note = [], [], None
        label = "[dry-run] " if self.dry_run else ""

        if state["incoming"]["attacks"]:
            log.warning("[%s] %d attack(s) incoming, first in %ss", self.vname,
                        state["incoming"]["attacks"], state["incoming"]["first_in"])
            self._defend(state, plan, None, actions)
            state = self.client.village()

        reserved = self._reservation(state, plan)
        if reserved:
            waits.append(reserved["eta"])

        if vid not in self.coords and plan["expansion"]["enabled"] \
                and rules.building_level(state, "Residence") >= 10:
            self.coords[vid] = self.client.coordinates()
        if not self.dry_run:
            note = self.expansion.step(self.client, vid, self.coords.get(vid), state, plan,
                                       self.tribe, village_count, reserved, self._ally_tiles())
            if note:
                log.info("[%s] %s", self.vname, note)
                state = self.client.village()

        # spare gold finishes long jobs first, so their build slot can take a new order right away
        if self._spend_gold(vid, state, plan, actions) and not self.dry_run:
            state = self.client.village()

        for pick in (rules.field_candidates, rules.building_candidates):
            while len(actions) < self.max_actions:
                placed = False
                for cand in pick(state, plan):
                    info = self._inspect(cand)
                    if info.get("href"):
                        if reserved and cand["slot"] != reserved["slot"] and info.get("cost") \
                                and rules.delays_reserved(reserved["cost"], state["resources"], info["cost"]):
                            log.debug("skip %s: saving for %s", cand["name"], reserved["name"])
                            continue
                        self._act(cand, info["href"], actions, label)
                        placed = True
                        break
                    if info.get("cost"):
                        waits.append(rules.seconds_until_affordable(info["cost"], state["resources"]))
                    if "workers" in (info.get("status") or "").lower():
                        break  # this queue is full; cheaper candidates won't help
                if not placed or self.dry_run:
                    break
                state = self.client.village()  # resources and queue changed

        if not state["incoming"]["attacks"]:
            self._defend(state, plan, reserved, actions)
        return state, actions, waits, reserved, note

    def tick(self, plan):
        """One round over all villages. Returns (status_villages, actions, sleep_seconds)."""
        first = self.client.village()
        villages = first["villages"] or [{"id": None, "name": "village", "x": None, "y": None, "current": True}]
        if time.time() - self.capital_checked > 6 * 3600:  # the capital rarely changes
            try:
                self.capital = self.client.capital_id()
            except (requests.RequestException, GameError):
                pass
            self.capital_checked = time.time()
        try:
            self.allies = ally_status(self.state_dir)
        except OSError as e:
            log.warning("could not read the allied bots' status: %s", e)
            self.allies = []
        for v in villages:  # a single village has no coordinates in the village list: ask the map once
            if v["x"] is None and self.coords.get(v["id"]) is None:
                try:
                    self.coords[v["id"]] = self.client.coordinates()
                except (requests.RequestException, GameError, ValueError):
                    pass
        out, all_actions, soon, seen = [], [], [], []
        for v in villages:
            self.vname = v["name"]
            if v["id"] is not None:
                self.client.select_village(v["id"])
                if v["x"] is not None:
                    self.coords[v["id"]] = (v["x"], v["y"])
                state = self.client.village()
            else:
                state = first
            state, actions, waits, reserved, note = self.village_tick(v["id"], state, plan, len(villages))
            seen.append({**v, "state": state, "reserved": reserved})
            all_actions += [{**a, "village": v["name"]} for a in actions]
            soon += [job["seconds_left"] + 10 for job in state["queue"]] + [w + 10 for w in waits if w > 0]
            if state["incoming"]["attacks"]:
                soon.append(ATTACK_SLEEP)
            out.append({
                "id": v["id"], "name": v["name"], "coords": self.coords.get(v["id"]),
                "resources": state["resources"], "queue": state["queue"], "fields": state["fields"],
                "buildings": {str(k): b for k, b in sorted(state["buildings"].items()) if b},
                "troops": {units.name(u): n for u, n in sorted(state["troops"].items())},
                "incoming": state["incoming"],
                "saving_for": {"name": reserved["name"], "eta_min": int(reserved["eta"] // 60)} if reserved else None,
                "expansion_note": note,
                "gold": state.get("gold"),
                "capital": v["id"] is not None and v["id"] == self.capital,
            })
        try:
            all_actions += self.share(seen, plan)
        except (requests.RequestException, GameError, ValueError, KeyError) as e:
            log.warning("resource sharing failed: %s", e)
        try:
            all_actions += self.reinforce(seen, plan)
        except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
            log.warning("reinforcement failed: %s", e)
        if self.diplomacy:
            try:
                msg = self.diplomacy.step(self.client, seen, self.dry_run)
                if msg:
                    log.info("diplomacy: %s", msg)
                    all_actions.append({"action": "diplomacy", "name": msg, "village": "", "reason": "",
                                        "time": int(time.time())})
            except (requests.RequestException, GameError, ValueError, KeyError) as e:
                log.warning("diplomacy failed: %s", e)
        sleep = min(soon) if soon else IDLE_SLEEP
        sleep = max(MIN_SLEEP, min(MAX_SLEEP, sleep)) + random.uniform(0, 20)
        return out, all_actions, sleep


def run_bot(state_dir, args, ready):
    """The loop for one account (runs in its own thread, named after the account)."""
    account = json.loads((state_dir / "bot-account.json").read_text(encoding="utf-8"))
    client = GameClient(os.environ.get("TRAVBOT_BASE_URL", "http://192.168.68.205"),
                        account["user"], account["pw"])
    tribe = int(account.get("tribe", 3))
    max_age = float(os.environ.get("TRAVBOT_PLAN_MAX_AGE_HOURS", "6"))
    ollama_url = os.environ.get("TRAVBOT_OLLAMA_URL", "http://192.168.68.87:11434")
    advisor = None
    if ollama_url:
        advisor = Advisor(
            ollama_url,
            os.environ.get("TRAVBOT_IDLE_URL", "http://192.168.68.87:8911/idle.json"),
            [m.strip() for m in os.environ.get("TRAVBOT_ADVISOR_MODELS", "gemma4:26b,ornith:9b").split(",") if m.strip()],
            state_dir,
            interval_s=60 * float(os.environ.get("TRAVBOT_ADVISOR_INTERVAL_MIN", "60")),
            retry_s=60 * float(os.environ.get("TRAVBOT_ADVISOR_RETRY_MIN", "10")),
            tribe=tribe,
        )

    def writer(kind, ctx):
        letter = None
        if advisor:
            try:
                letter = advisor.letter(kind, ctx)
            except Exception:  # a letter must never stop the bot
                log.exception("AI letter failed")
        return letter or template_letter(kind, ctx)

    bot = Bot(client, tribe, state_dir, int(os.environ.get("TRAVBOT_MAX_ACTIONS", "6")), args.dry_run,
              Diplomacy(state_dir, account["user"], writer))
    ready.set()

    force_advice = args.advise_now
    history = []
    while True:
        plan, source = planmod.load(state_dir / "plan.json", max_age)
        status = None
        try:
            villages, actions, sleep = bot.tick(plan)
            history = (history + actions)[-50:]
            status = {"updated": int(time.time()), "account": account["user"], "plan_source": source,
                      "tribe": bot.tribe, "villages": villages, "expansion": bot.expansion.summary(),
                      "diplomacy": bot.diplomacy.summary() if bot.diplomacy else None,
                      "recent_actions": history, "next_tick_in": int(sleep)}
            if not args.dry_run:
                write_json(state_dir / "status.json", status)
            log.info("tick done: %d order(s) in %d village(s), plan=%s, next tick in %ds",
                     len(actions), len(villages), source, sleep)
        except Exception as e:  # one account's failure must not stop its thread (or the other bots)
            log.exception("tick failed: %s", e)
            sleep = ERROR_SLEEP

        if advisor and status and not args.dry_run:
            try:
                msg = advisor.maybe_run(status, plan, force=force_advice)
            except Exception:  # the advisor must never stop the bot
                log.exception("advisor crashed")
                msg = None
            if msg:
                log.info("%s", msg)
            force_advice = False
        if args.once:
            return
        time.sleep(sleep)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--once", action="store_true", help="run a single tick and exit")
    ap.add_argument("--dry-run", action="store_true", help="decide but don't place orders")
    ap.add_argument("--advise-now", action="store_true", help="ask the advisor after the first tick, skipping the idle check")
    ap.add_argument("--only", help="run only this account (folder name, e.g. state-roman)")
    args = ap.parse_args()

    primary = pathlib.Path(os.environ.get("TRAVBOT_STATE_DIR", "state")).resolve()
    primary.mkdir(parents=True, exist_ok=True)
    setup_logging()
    dirs = discover_state_dirs(primary)
    if args.only:
        dirs = [d for d in dirs if d.name == args.only]

    bots = []
    for i, d in enumerate(dirs):
        name = json.loads((d / "bot-account.json").read_text(encoding="utf-8"))["user"]
        add_bot_log(d, name, also_main=(i == 0))
        bots.append((name, d))

    web_port = int(os.environ.get("TRAVBOT_WEB_PORT", "8080"))
    if web_port and not args.once:
        web.serve(bots, web_port)

    threads = []
    for i, (name, d) in enumerate(bots):
        ready = threading.Event()
        th = threading.Thread(target=run_bot, args=(d, args, ready), name=name, daemon=True)
        th.start()
        ready.wait(60)
        threads.append(th)
        if i + 1 < len(bots):
            time.sleep(20)  # stagger the accounts a little
    log.info("running %d bot(s): %s", len(bots), ", ".join(n for n, _ in bots))
    for th in threads:
        th.join()


if __name__ == "__main__":
    main()
