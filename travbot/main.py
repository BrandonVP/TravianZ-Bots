"""Bot entry point: python -m travbot.main [--once] [--dry-run] [--advise-now]

Environment:
  TRAVBOT_BASE_URL            game URL (default http://192.168.68.205)
  TRAVBOT_STATE_DIR           folder with bot-account.json, plan.json, status.json, bot.log (default ./state);
                              every sibling folder state-* with a bot-account.json is another bot account,
                              run in its own thread (its own plan, logs and status page /b/<name>)
  TRAVBOT_PLAN_MAX_AGE_HOURS  plan.json older than this is ignored (default 6)
  TRAVBOT_MAX_ACTIONS         build orders per village per tick at most (default 6)
  TRAVBOT_OLLAMA_URL          Ollama for the advisor (default http://192.168.68.87:11434; empty disables)
  TRAVBOT_MAIL_AI             1 = bots answer players' letters and thank for resources with the model (travbot/mailer.py)
  TRAVBOT_MAIL_MODELS         models for letters (default: TRAVBOT_ADVISOR_MODELS) - a small fast one is enough
  TRAVBOT_IDLE_URL            idle check (default http://192.168.68.87:8911/idle.json; empty = always-on Ollama server)
  TRAVBOT_OLLAMA_YIELD        1 = skip a review while Ollama has another model loaded (default); 0 = shared server, just ask
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
from .mailer import Mailer
from .client import GameClient, GameError, tile_id
from .diplomacy import Diplomacy, join_list, template_letter
from .humans import HumanAllies
from .expand import Expansion
from .raid import RaidBook, raider_choices, scout_unit, form_slot, dist as raid_dist
from .champion import ChampionKeeper
from .world import WorldClock, NATAR_ATTACK_DAY
from .intel import Hostiles
from .army import Army
from .roles import Roles, overlay
from .wonder import Support, wonder_villages, relic_villages
from .letters import Requests
from .champions import ChampionPlan

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
        self.archers = False       # server has the Archery Range units (checked hourly)
        self.archers_checked = 0.0
        self.raid = RaidBook(state_dir)
        self.raiders = {}          # village id -> raider unit id the village can train (None: none yet)
        self.champion = ChampionKeeper(client, dry_run)
        self.champion_checked = 0.0
        self.humans = HumanAllies(state_dir)   # human members of the alliance: gifts and defence
        # plan 2026-11-13-smarter-bots: world phase, village roles, army, hostile list, Wonder/relic support
        self.world = WorldClock()
        self.day, self.phase = None, "growth"
        self.hostiles = Hostiles(state_dir)
        self.army = Army(client, tribe, state_dir, self.hostiles, dry_run)
        self.roles = Roles(state_dir)
        self.role_of = {}          # village id -> capital / army / feeder
        self.support = Support(client, tribe, state_dir, dry_run)
        self.champions = ChampionPlan(client, tribe, state_dir, dry_run)   # war / steward / guardian
        self.champ_extra = {}      # units the army village keeps so the next champion can be made
        self.last_seen = []        # last round's villages with their state (for letters / requests)
        self.last_plan = None

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
            eta = rules.seconds_until_affordable(info["cost"], rules.stock(state))
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
                                  d["unit_target"], rules.stock(state), share)
        count = min(count, info["max"])
        if count <= 0:
            return
        spend = {k: c * count for k, c in info["cost"].items()}
        if reserved and not under_attack and rules.delays_reserved(reserved["cost"], rules.stock(state), spend):
            return
        log.info("[%s] %strain %d %s in Barracks (slot %d)%s", self.vname, "[dry-run] " if self.dry_run else "",
                 count, units.name(unit), slot, " - ATTACK INCOMING" if under_attack else "")
        if not self.dry_run:
            self.client.train(slot, unit, count)
        actions.append({"action": "train", "name": units.name(unit), "count": count, "slot": slot,
                        "reason": "attack incoming" if under_attack else f"target {d['unit_target']}",
                        "time": int(time.time())})

    def _defend_ranged(self, state, plan, reserved, actions):
        """Servers with archers: research the tribe's skirmisher (anti-archer defence) in the Academy
        and keep `defense.ranged_target` of them, trained at the Archery Range."""
        d = plan["defense"]
        if not plan.get("archers") or d["ranged_target"] <= 0:
            return
        slot = rules.find_slot(state, "Archery Range")
        if slot is None:
            return
        unit = units.skirmisher(self.tribe)
        page = self.client.train_page(slot)
        info = page["units"].get(unit)
        if info is None:
            # not researched yet: start it when the Academy offers it (Academy 5 + Archery Range 5)
            academy = rules.find_slot(state, "Academy")
            if academy is None or rules.building_level(state, "Academy") < 5                     or rules.building_level(state, "Archery Range") < 5:
                return
            href = self.client.research_link(academy, unit)
            if not href:
                return
            log.info("[%s] %sresearch %s in the Academy (slot %d)", self.vname,
                     "[dry-run] " if self.dry_run else "", units.name(unit), academy)
            if not self.dry_run:
                self.client.follow(href)
            actions.append({"action": "research", "name": units.name(unit), "slot": academy,
                            "reason": "archer defence", "time": int(time.time())})
            return
        count = rules.troop_order(info["cost"], state["troops"].get(unit, 0), page["training"].get(unit, 0),
                                  d["ranged_target"], rules.stock(state), d["budget_share"])
        count = min(count, info["max"])
        if count <= 0:
            return
        spend = {k: c * count for k, c in info["cost"].items()}
        if reserved and rules.delays_reserved(reserved["cost"], rules.stock(state), spend):
            return
        log.info("[%s] %strain %d %s at the Archery Range (slot %d)", self.vname,
                 "[dry-run] " if self.dry_run else "", count, units.name(unit), slot)
        if not self.dry_run:
            self.client.train(slot, unit, count)
        actions.append({"action": "train", "name": units.name(unit), "count": count, "slot": slot,
                        "reason": f"target {d['ranged_target']}", "time": int(time.time())})

    def _spend_gold(self, vid, state, plan, actions):
        """Finish good-value running jobs with spare village gold. Returns True if something was finished."""
        g = plan["gold"]
        reserve = g["capital_reserve"] if vid is not None and vid == self.capital else g["reserve"]
        done = False
        for job in rules.finish_choices(state["queue"], state.get("gold"), reserve, g):
            log.info("[%s] %sfinish %s -> level %s now for %s gold (%d min left, gold %d, keeping %d)",
                     self.vname, "[dry-run] " if self.dry_run else "", display(job["name"]), job["to_level"],
                     job["finish_price"], job["seconds_left"] // 60, state["gold"]["amount"], reserve)
            if not self.dry_run:
                self.client.finish_job(job["finish_href"])
            actions.append({"action": "finish", "name": display(job["name"]), "slot": job["slot"],
                            "reason": f"{job['seconds_left'] // 60} min left, {job['finish_price']} gold",
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
        # playbook: feeders supply the army village(s) once the army is being built (growth phase on)
        if self.phase in ("growth", "relics", "wonder"):
            for v in villages:
                st = v["state"]
                fill = sum(st["resources"][k]["amount"] / max(1, st["resources"][k]["cap"])
                           for k in ("wood", "clay", "iron", "crop")) / 4
                if self.role_of.get(v["id"]) == "army" and v not in young and v["x"] is not None \
                        and not st["incoming"]["attacks"] and fill < sp["fill_to"] * 0.5:
                    young.append(v)
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
                log.info("[%s] %ssend %s to %s (%s|%s): %s", d["name"],
                         "[dry-run] " if self.dry_run else "",
                         ", ".join(f"{k} {n}" for k, n in ship.items() if n), r["name"], r["x"], r["y"],
                         "young allied village" if r.get("ally") else
                         "army village" if self.role_of.get(r["id"]) == "army" else "young village")
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
        if d.get("reinforce_humans", True):
            targets += self.humans.targets(self.client.user)
        targets = [t for t in targets if t["coords"] and (t["incoming"].get("attacks") or 0) > 0]
        if not targets:
            return []
        path = self.state_dir / "reinforce.json"
        try:
            sent = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            sent = {}
        unit = rules.basic_defender(self.tribe)
        skirm = units.skirmisher(self.tribe) if plan.get("archers") else None
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
                count = max(0, int((v["state"]["troops"].get(unit, 0) - d["reinforce_keep"]) * d["reinforce_share"]))
                # skirmishers go along: half of those above half the village's target
                extra = {}
                if skirm:
                    n = int((v["state"]["troops"].get(skirm, 0) - d["ranged_target"] // 2) * d["reinforce_share"])
                    if n >= 1:
                        extra[units.extra_slot(skirm)] = n
                if count < 1 and not extra:
                    continue
                what = join_list([f"{count} {units.name(unit)}"] * (count > 0)
                                 + [f"{n} {units.name(skirm)}" for n in extra.values()])
                self.vname = v["name"]
                log.info("[%s] %sreinforce %s (%s|%s) with %s: attack incoming", v["name"],
                         "[dry-run] " if self.dry_run else "", t["name"], tx, ty, what)
                ok = True
                if not self.dry_run:
                    if v["id"] is not None:
                        self.client.select_village(v["id"])
                    ok = self.client.send_reinforcement(tx, ty, 1, count, unit if count > 0 else skirm, extra)
                if not ok:
                    log.info("[%s] the game refused the reinforcement to %s", v["name"], t["name"])
                    continue
                sent[key] = now
                try:
                    self.humans.note_reinforcement(self.client, t, what, self.client.user, self.dry_run,
                                                   count + sum(extra.values()))
                except (requests.RequestException, GameError) as e:
                    log.warning("could not write to %s: %s", t.get("human"), e)
                actions.append({"action": "reinforce", "name": f"{what} to {t['name']}",
                                "slot": 39, "reason": "attack incoming", "village": v["name"], "time": int(now)})
        if not self.dry_run:
            write_json(path, {k: ts for k, ts in sent.items() if now - ts < 24 * 3600})
        return actions

    def champion_tick(self, villages, plan, roles=None, quiet=None, attacked=False):
        """Every plan["champion"]["every_minutes"]: the champion's rules (travbot/champion.py), run from
        the first village with a Champion's Hall."""
        cfg = plan["champion"]
        if not cfg["enabled"] or time.time() - self.champion_checked < cfg["every_minutes"] * 60:
            return []
        self.champion_checked = time.time()
        for v in villages:
            slot = next((int(s) for s, b in v["state"]["buildings"].items() if b and b["name"] == "Hero's Mansion"), None)
            if slot is None:
                continue
            if v["id"] is not None:
                self.client.select_village(v["id"])
            self.vname = v["name"]
            acts = self.champion.step(slot, cfg, roles, quiet, attacked)
            now = int(time.time())
            return [{**a, "village": v["name"], "time": now} for a in acts]
        return []

    def raids(self, villages, plan):
        """Raid safe targets (travbot/raid.py): learn from new reports, refresh the map once a day,
        check oases for animals, then send scouts/raids from villages with raiders at home."""
        r = plan["raid"]
        if not r["enabled"]:
            return []
        book, now, actions = self.raid, time.time(), []
        me = self.client.user
        ally_names = {a.get("account") for a in self.allies if a.get("account")}
        own = {v["name"] for v in villages}
        raider_ids = {u for u in self.raiders.values() if u}
        scout = scout_unit(self.tribe)

        # 1. reports of our own raids and scouting missions
        seen = set(book.data["seen_reports"])
        for rep in self.client.reports():
            if rep["id"] in seen:
                continue
            seen.add(rep["id"])
            book.data["seen_reports"].append(rep["id"])
            topic = rep["topic"]
            # our own missions: learn() matches them by target village against what we sent
            # (a single-village account has no village names in the list to match the topic with)
            if not book.data["out"] or (" attacks " not in topic and " scouts " not in topic)                     or any(topic.endswith(" " + n) for n in own):
                continue
            parsed = self.client.report(rep["id"])
            if parsed:
                line = book.learn(parsed, raider_ids, scout, r, now)
                if line:
                    log.info("raid: %s", line)

        # 2. the map around every village, once a day
        coords = [tuple(self.coords[v["id"]]) for v in villages if self.coords.get(v["id"])]
        own_alliance = None
        if coords and book.scan_due(24, now):
            done = set()
            for (x, y) in coords:
                for cx, cy in ((x - 6, y - 6), (x + 6, y - 6), (x - 6, y + 6), (x + 6, y + 6)):
                    if (cx, cy) in done:
                        continue
                    done.add((cx, cy))
                    tiles = self.client.map_block(cx, cy)
                    book.observe(tiles, now)
            log.info("raid: map scanned, %d villages and %d free oases known",
                     len(book.data["villages"]), len(book.data["oases"]))
        for v in book.data["villages"].values():
            if v.get("owner") == me and v.get("alliance"):
                own_alliance = v["alliance"]
                break

        # 3. a few oases' animals (unoccupied oases are only worth it when empty)
        for (x, y) in coords:
            for key in book.oases_to_check((x, y), r["radius"], r["rescout_hours"], 3, now):
                o = book.data["oases"][key]
                book.set_animals(key, self.client.oasis_animals(o["x"], o["y"]), now)

        # 4. send
        for v in villages:
            c = self.coords.get(v["id"])
            st = v["state"]
            if not c or st["incoming"]["attacks"] or not rules.find_slot(st, "Rally Point"):
                continue
            room = r["max_out"] - len(book.out_from(v["id"], now))
            if room <= 0:
                continue
            # the best raider actually at home (defenders stay home), else the one the village trains
            raider, spare = self.raiders.get(v["id"]), 0
            for u in raider_choices(self.tribe):
                keep = plan["defense"]["unit_target"] if u == rules.basic_defender(self.tribe) else 0
                n = st["troops"].get(u, 0) - keep
                if n >= r["min_wave"]:
                    raider, spare = u, n
                    break
            scouts = st["troops"].get(scout, 0) if scout else 0
            for t in book.plan_raids(tuple(c), r, me, ally_names, own_alliance, now):
                if room <= 0:
                    break
                if t["action"] == "scout":
                    if scouts < r["scout_count"]:
                        continue
                    n, unit, mode = r["scout_count"], scout, 3
                else:
                    n = min(book.wave(t["key"], r), spare)
                    if not raider or n < r["min_wave"]:
                        continue
                    unit, mode = raider, 4
                what = f"{t['action']} {t['name']} ({t['x']}|{t['y']}, {t['kind']}"
                what += f", {t['owner']})" if t.get("owner") else ")"
                self.vname = v["name"]
                log.info("[%s] %s%s with %d %s", v["name"], "[dry-run] " if self.dry_run else "", what, n,
                         units.plural(units.name(unit)) if n != 1 else units.name(unit))
                ok = True
                if not self.dry_run:
                    if v["id"] is not None:
                        self.client.select_village(v["id"])
                    ok = self.client.send_troops(t["x"], t["y"], mode, {form_slot(unit): n}, check_unit=unit)
                if not ok:
                    log.info("[%s] the game refused the %s on %s; skipping it for a day", v["name"], t["action"], t["name"])
                    book.data["targets"].setdefault(t["key"], {})["blocked_until"] = now + 86400
                    continue
                if not self.dry_run:
                    book.sent(v["id"], t["key"], t["action"], n, now)
                room -= 1
                if mode == 3:
                    scouts -= n
                else:
                    spare -= n
                actions.append({"action": t["action"], "name": f"{n} {units.name(unit)} to {t['name']}",
                                "slot": 39, "reason": t["kind"], "village": v["name"], "time": int(now)})
        book.forget_old(now)
        if not self.dry_run:
            book.save()
        return actions

    def village_tick(self, vid, state, plan, village_count):
        """Place orders for one village. Returns (state, actions, waits, reserved, note)."""
        actions, waits, note = [], [], None
        label = "[dry-run] " if self.dry_run else ""
        base_plan = plan
        role = self.role_of.get(vid, "feeder")
        plan = overlay(plan, role, self.phase, guardian=(vid is not None and vid == self.roles.data.get("guardian")))

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
                                and rules.delays_reserved(reserved["cost"], rules.stock(state), info["cost"]):
                            log.debug("skip %s: saving for %s", cand["name"], reserved["name"])
                            continue
                        self._act(cand, info["href"], actions, label)
                        placed = True
                        break
                    if info.get("cost"):
                        waits.append(rules.seconds_until_affordable(info["cost"], rules.stock(state)))
                    if "workers" in (info.get("status") or "").lower():
                        break  # this queue is full; cheaper candidates won't help
                if not placed or self.dry_run:
                    break
                state = self.client.village()  # resources and queue changed

        if not state["incoming"]["attacks"]:
            self._defend(state, plan, reserved, actions)
            try:
                self._defend_ranged(state, plan, reserved, actions)
            except (requests.RequestException, GameError, ValueError, KeyError) as e:
                log.warning("[%s] archer defence failed: %s", self.vname, e)
            try:
                self._train_raiders(vid, state, plan, reserved, actions)
            except (requests.RequestException, GameError, ValueError, KeyError) as e:
                log.warning("[%s] raider training failed: %s", self.vname, e)
            try:
                self.army.village_step(vid, state, role, base_plan, self.phase, reserved, actions, village_count,
                                       max(1, sum(1 for r in self.role_of.values() if r == "army")),
                                       self.champ_extra if role == "army" else None)
            except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
                log.warning("[%s] army work failed: %s", self.vname, e)
        return state, actions, waits, reserved, note

    def _train_page_for(self, state, unit):
        """(slot, train page) of the Barracks or Stable that offers `unit`, or (None, None)."""
        for building in ("Barracks", "Stable"):
            slot = rules.find_slot(state, building)
            if slot is None:
                continue
            page = self.client.train_page(slot)
            if unit in page["units"]:
                return slot, page
        return None, None

    def _order(self, state, plan, reserved, actions, unit, target, share, why):
        """Train `unit` up to `target` (counting those at home) within `share` of each resource."""
        slot, page = self._train_page_for(state, unit)
        if slot is None:
            return False
        info = page["units"][unit]
        count = min(rules.troop_order(info["cost"], state["troops"].get(unit, 0), page["training"].get(unit, 0),
                                      target, rules.stock(state), share), info["max"])
        if count <= 0:
            return True
        spend = {k: c * count for k, c in info["cost"].items()}
        if reserved and rules.delays_reserved(reserved["cost"], rules.stock(state), spend):
            return True
        log.info("[%s] %strain %d %s (slot %d) for %s", self.vname, "[dry-run] " if self.dry_run else "",
                 count, units.name(unit), slot, why)
        if not self.dry_run:
            self.client.train(slot, unit, count)
        actions.append({"action": "train", "name": units.name(unit), "count": count, "slot": slot,
                        "reason": why, "time": int(time.time())})
        return True

    def _train_raiders(self, vid, state, plan, reserved, actions):
        """Raiding villages (with a Rally Point) keep `raid.unit_target` raiders on top of their
        defenders, and a few scouts for looking at targets first."""
        r = plan["raid"]
        if not r["enabled"] or r["unit_target"] <= 0 or not rules.find_slot(state, "Rally Point"):
            return
        defender = rules.basic_defender(self.tribe)
        raider = None
        for u in raider_choices(self.tribe):
            slot, _ = self._train_page_for(state, u)
            if slot is not None:
                raider = u
                break
        self.raiders[vid] = raider
        if raider is not None:
            extra = plan["defense"]["unit_target"] if raider == defender else 0
            self._order(state, plan, reserved, actions, raider, r["unit_target"] + extra, r["budget_share"], "raiding")
        scout = scout_unit(self.tribe)
        if scout:
            self._order(state, plan, reserved, actions, scout, 2 * r["scout_count"], r["budget_share"], "scouting")

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
        if time.time() - self.archers_checked > 3600:
            try:
                self.archers = self.client.archers_enabled()
            except (requests.RequestException, GameError):
                pass
            self.archers_checked = time.time()
        plan = {**plan, "archers": self.archers}
        self.last_plan = plan
        self.day = self.world.day(self.client)
        from .world import phase as _phase
        self.phase = _phase(self.day)
        known = self.last_seen
        if not known:   # first round: last run's village states from status.json, else just the village list
            try:
                prev = {v["id"]: v for v in json.loads((self.state_dir / "status.json").read_text(encoding="utf-8"))["villages"]}
            except (OSError, ValueError, KeyError, TypeError):
                prev = {}
            known = [{"id": v["id"], "state": {"resources": (prev.get(v["id"]) or {}).get("resources")
                                                or {k: {"prod": 0} for k in ("wood", "clay", "iron", "crop")}}}
                     for v in villages]
        self.role_of = self.roles.assign(known, self.capital)
        if plan["champion"]["enabled"] and plan["champion"]["strategy"]:
            best_hall = max((rules.building_level(v["state"], "Hero's Mansion") for v in self.last_seen), default=0)
            self.champ_extra = self.champions.wanted_units(best_hall, self.phase)
        log.info("world day %s, phase %s; roles: %s", self.day or "?", self.phase,
                 ", ".join(f"{v['name']} {self.role_of.get(v['id'])}" for v in villages
                           if self.role_of.get(v["id"]) != "feeder") or "all feeders")
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
                "id": v["id"], "name": v["name"], "coords": self.coords.get(v["id"]), "role": self.role_of.get(v["id"]),
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
        self.last_seen = seen
        try:  # the alliance's human members: read once per round for gifts and reinforcement targets
            if self.humans.refresh(self.client):
                for who, victim in self.hostiles.observe(self.humans.ally):
                    log.info("hostile: %s attacks %s", who, victim)
                all_actions += self.humans.gifts(self.client, seen, plan, self.client.user, self.coords, self.dry_run)
        except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
            log.warning("help for human members failed: %s", e)
        try:  # reports: what attackers sent, what our scouts saw
            self.army.learn([v["name"] for v in seen])
        except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
            log.warning("reading reports failed: %s", e)
        try:
            all_actions += self.reinforce(seen, plan)
        except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
            log.warning("reinforcement failed: %s", e)
        try:
            all_actions += self.raids(seen, plan)
        except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
            log.warning("raiding failed: %s", e)
        try:
            all_actions += self.champion_tick(seen, plan, self.champions.roles(), self.champions.quiet_jobs(),
                                              bool((self.humans.ally or {}).get("incoming")))
        except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
            log.warning("champion failed: %s", e)
        try:  # timed sends from members' letters, then the counter-strike, then the Wonder / relic support
            all_actions += self.army.run_schedule()
            members = {m["name"] for m in (self.humans.ally or {}).get("members", []) if not m.get("bot")}
            map_pop = {int(k): v.get("pop") for k, v in self.raid.data.get("villages", {}).items()}
            all_actions += self.army.strikes(seen, self.role_of, self.coords, plan, self.client.user, members, map_pop,
                                             war_champ=self.champions.war_champion)
        except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
            log.warning("army failed: %s", e)
        try:
            natars = bool(self.day and self.day >= NATAR_ATTACK_DAY)
            all_actions += self.support.step(seen, self.coords, plan, self.humans.ally, natars)
        except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
            log.warning("Wonder/relic support failed: %s", e)
        if plan["champion"]["enabled"] and plan["champion"]["strategy"]:
            try:   # three champions with jobs: train, move home, clear oases (travbot/champions.py)
                self.champions.note_attacks(self.humans.ally, {v["id"] for v in seen})
                guardian = self.champions.guardian_village(seen, self.role_of, self.coords,
                                                           wonder_villages(self.humans.ally))
                if not self.dry_run:
                    self.roles.set_guardian(guardian)
                all_actions += self.champions.step(seen, self.role_of, self.coords, self.phase, self.capital, guardian)
                for v in seen:
                    if self.role_of.get(v["id"]) != "army":
                        continue
                    from . import combat as _combat
                    offence = {u: v["state"]["troops"].get(u, 0) for u in _combat.offence_units(self.tribe)}
                    all_actions += self.champions.clear_oasis(v, self.coords, {u: n for u, n in offence.items() if n},
                                                              self.raid, plan,
                                                              rules.building_level(v["state"], "Hero's Mansion"))
            except (requests.RequestException, GameError, ValueError, KeyError, TypeError) as e:
                log.warning("champions failed: %s", e)
        self.army.save()
        nxt = self.army.next_departure()
        if nxt is not None:
            soon.append(max(15, nxt - time.time() - 30))
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
        floor = 15 if nxt is not None and nxt - time.time() < MIN_SLEEP else MIN_SLEEP   # a timed send is near
        sleep = max(floor, min(MAX_SLEEP, sleep)) + random.uniform(0, 20 if floor == MIN_SLEEP else 2)
        return out, all_actions, sleep


def _alliance_summary(feed):
    if not feed or not feed.get("alliance"):
        return None
    members = feed.get("members", [])
    return {"name": feed["alliance"].get("name"), "members": len(members),
            "humans": sum(1 for m in members if not m.get("bot")),
            "wonders": len(wonder_villages(feed)), "relics": len(relic_villages(feed))}


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
            yield_to_others=os.environ.get("TRAVBOT_OLLAMA_YIELD", "1") != "0",
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
    # players' letters + thank-yous for resources, written by the local model (travbot/mailer.py)
    mailer = None
    if ollama_url and os.environ.get("TRAVBOT_MAIL_AI", "") == "1" and not args.dry_run:
        all_bots = []
        for d in state_dir.parent.glob("state*"):
            try:
                all_bots.append(json.loads((d / "bot-account.json").read_text(encoding="utf-8"))["user"])
            except (OSError, ValueError, KeyError):
                pass
        mail_models = os.environ.get("TRAVBOT_MAIL_MODELS") or os.environ.get("TRAVBOT_ADVISOR_MODELS", "gemma4:26b,ornith:9b")
        mailer = Mailer(client, account["user"], tribe, state_dir, ollama_url,
                        [m.strip() for m in mail_models.split(",") if m.strip()], all_bots)
        mailer.requests = Requests(bot, ollama_url, [m.strip() for m in mail_models.split(",") if m.strip()])
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
                      "raid": bot.raid.summary(),
                      "world": {"day": bot.day, "phase": bot.phase, "relics_day": bot.world.info()["relics_day"],
                                "wonder_day": bot.world.info()["wonder_day"]},
                      "hostiles": bot.hostiles.summary(plan["counter"]["hostile_days"]),
                      "army": bot.army.summary(),
                      "champions": bot.champions.summary(),
                      "alliance": _alliance_summary(bot.humans.ally),
                      "diplomacy": bot.diplomacy.summary() if bot.diplomacy else None,
                      "recent_actions": history, "next_tick_in": int(sleep)}
            if not args.dry_run:
                write_json(state_dir / "status.json", status)
            log.info("tick done: %d order(s) in %d village(s), plan=%s, next tick in %ds",
                     len(actions), len(villages), source, sleep)
        except Exception as e:  # one account's failure must not stop its thread (or the other bots)
            log.exception("tick failed: %s", e)
            sleep = ERROR_SLEEP

        if mailer and status:
            mailer.tick()

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


def _restart_watcher(flag):
    """Restart without TrueNAS: when the file RESTART appears next to the code (bot/RESTART on the share), delete it
    and exit; Docker's restart policy starts the container again with fresh code. Only exits if the file could be
    deleted, so a stuck file can never cause a restart loop."""
    warned = False
    while True:
        try:
            if flag.exists():
                flag.unlink()
                log.info("RESTART file found - exiting so Docker restarts the bot with fresh code")
                logging.shutdown()
                os._exit(0)
        except OSError as e:
            if not warned:
                log.error("RESTART file found but could not be deleted (%s) - not restarting", e)
                warned = True
        time.sleep(5)


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
    if not args.once:
        threading.Thread(target=_restart_watcher, args=(primary.parent / "RESTART",), name="restart-watch", daemon=True).start()
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

    # The Sentry (AI sitter, travbot/sentry.py): one extra thread, only when its folder holds sentry.json
    sentry_dir = pathlib.Path(os.environ.get("TRAVBOT_SENTRY_DIR", str(primary.parent / "sentry")))
    if (sentry_dir / "sentry.json").is_file() and not args.only:
        from .sentry import Sentry
        sentry = Sentry(sentry_dir, dry_run=args.dry_run)
        th = threading.Thread(target=sentry.run, kwargs={"once": args.once}, name="Sentry", daemon=True)
        th.start()
        threads.append(th)
    for th in threads:
        th.join()


if __name__ == "__main__":
    main()
