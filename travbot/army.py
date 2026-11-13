"""The army (plan 2026-11-13-smarter-bots): an attack army in the army village, research and upgrades, Town Hall
celebrations, and the counter-strike against players who attack the alliance.

Fair play (Brandon 2026-11-13: no heavy raiding of active players; "you want an ally that has your back"):
- the bots attack a human only when that player is on the hostile list (attacked a bot or a member within
  `counter.hostile_days`), at most `per_target_per_day` strikes on one player (all bots together) and
  `per_bot_per_day` per bot;
- never a village below `min_target_pop`, never a player under beginners protection (the game refuses anyway);
- normal attacks with infantry/cavalry and rams only: no catapults and no chiefs against humans;
- the force is sized to win with a margin and to roughly what the attacker sent (`size_factor`), not more;
- a scouting run comes first, so the bots don't throw troops at an army they can't beat.
Catapults are trained from the relics phase for Etruscan (Natar) targets only (relics, Wonder support).

Timed sends ("land at 20:00", from letters) are kept in army.json and fired when due; the game has no scheduled
departure, so the bot reads the travel time from the confirmation page and waits.
"""
import json
import logging
import math
import time

from . import combat, rules, units
from .unitstats import STATS

log = logging.getLogger("travbot")

PHASE_ARMY = {"opening": 0, "second village": 0, "growth": 1.0, "relics": 2.0, "wonder": 3.0}
PER_VILLAGE_CAP = 250        # attack units per village the bot owns, whatever the plan says (economy first)
SCOUT_WAIT_S = 3 * 3600      # a scouting run without a report after this long counts as lost
STRIKE_RETRY_S = 6 * 3600    # a target that was too strong / refused is looked at again after this


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


class Army:
    def __init__(self, client, tribe, state_dir, hostiles, dry_run):
        self.c = client
        self.tribe = tribe
        self.dry_run = dry_run
        self.hostiles = hostiles
        self.path = state_dir / "army.json"
        self.data = _load(self.path, {})
        for k, v in (("scouted", {}), ("scouting", {}), ("skip", {}), ("schedule", []), ("seen_reports", []),
                     ("log", [])):
            self.data.setdefault(k, v)
        self.upgrades_checked = 0.0

    def save(self):
        if self.dry_run:
            return
        self.data["seen_reports"] = self.data["seen_reports"][-400:]
        self.data["log"] = self.data["log"][-50:]
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def _note(self, text):
        log.info("army: %s", text)
        self.data["log"].append({"time": int(time.time()), "text": text})

    # --- per village: training, research, upgrades, celebrations ---------------------------------------

    def targets(self, plan, phase, villages=1, armies=1):
        """{unit id: number to keep} in one army village for this phase (`villages`: the bot's village count,
        `armies`: its number of army villages)."""
        a = plan["army"]
        if not a["enabled"]:
            return {}
        size = min(int(a["unit_target"] * PHASE_ARMY.get(phase, 1.0)), PER_VILLAGE_CAP * villages) // max(1, armies)
        if size <= 0:
            return {}
        offence = combat.offence_units(self.tribe)
        inf = [u for u in offence if u not in combat.CAVALRY]
        cav = [u for u in offence if u in combat.CAVALRY]
        out = {}
        if inf:
            out[inf[0]] = int(size * (1 - a["cavalry_share"]))
        if cav:
            out[cav[0]] = int(size * a["cavalry_share"])
        out[combat.unit_id(self.tribe, 7)] = int(size * a["ram_share"])           # rams: walls
        if phase in ("relics", "wonder"):
            out[combat.unit_id(self.tribe, 8)] = int(size * a["catapult_share"])  # catapults: Etruscan targets only
        scout = combat.SCOUTS.get(self.tribe)
        if scout:
            out[scout] = a["scouts"]
        return {u: n for u, n in out.items() if n > 0}

    def village_step(self, vid, state, role, plan, phase, reserved, actions, villages=1, armies=1, extra=None):
        """Army work for one village (called from Bot.village_tick). `extra`: {unit: n} also wanted at home
        (the units the next champion is made from)."""
        if role == "army":
            want = self.targets(plan, phase, villages, armies)
            for u, n in (extra or {}).items():
                want[u] = max(want.get(u, 0), n)
            self._research(state, want, actions)
            for u, n in want.items():
                self._train(state, plan, reserved, actions, u, n)
            self._upgrade(state, "Blacksmith", [u for u in want if u != combat.SCOUTS.get(self.tribe)], actions, reserved)
        if role in ("capital", "army"):
            self._upgrade(state, "Armoury", [rules.basic_defender(self.tribe)], actions, reserved)
        self._celebrate(vid, state, plan, phase, actions)

    def _train_slot(self, state, uid):
        building = {7: "Workshop", 8: "Workshop"}.get(combat.slot_of(uid)) or \
            ("Stable" if uid in combat.CAVALRY else "Barracks")
        return rules.find_slot(state, building)

    def _train(self, state, plan, reserved, actions, uid, target):
        slot = self._train_slot(state, uid)
        if slot is None:
            return
        page = self.c.train_page(slot)
        info = page["units"].get(uid)
        if info is None or not info.get("cost"):
            return
        count = rules.troop_order(info["cost"], state["troops"].get(uid, 0), page["training"].get(uid, 0),
                                  target, rules.stock(state), plan["army"]["budget_share"])
        count = min(count, info["max"])
        if count <= 0:
            return
        spend = {k: c * count for k, c in info["cost"].items() if k in rules.stock(state)}
        if reserved and rules.delays_reserved(reserved["cost"], rules.stock(state), spend):
            return
        log.info("army: %strain %d %s (slot %d), keeping %d", "[dry-run] " if self.dry_run else "", count,
                 units.name(uid), slot, target)
        if not self.dry_run:
            self.c.train(slot, uid, count)
        actions.append({"action": "train", "name": units.name(uid), "count": count, "slot": slot,
                        "reason": f"army {target}", "time": int(time.time())})

    def _research(self, state, want, actions):
        """Academy: research the first wanted unit the village can't train yet (one research at a time)."""
        academy = rules.find_slot(state, "Academy")
        if academy is None:
            return
        for uid in want:
            if combat.slot_of(uid) == 1:
                continue  # the first unit needs no research
            slot = self._train_slot(state, uid)
            if slot is not None and uid in self.c.train_page(slot)["units"]:
                continue
            href = self.c.research_link(academy, uid)
            if not href:
                continue
            log.info("army: %sresearch %s in the Academy", "[dry-run] " if self.dry_run else "", units.name(uid))
            if not self.dry_run:
                self.c.follow(href)
            actions.append({"action": "research", "name": units.name(uid), "slot": academy, "reason": "army",
                            "time": int(time.time())})
            return

    def _upgrade(self, state, building, uids, actions, reserved):
        """Blacksmith (attack) / Armoury (defence): upgrade the lowest of `uids`. Upgrades count for every village."""
        slot = rules.find_slot(state, building)
        if slot is None or not uids:
            return
        page = self.c.upgrades(slot)
        rows = sorted((page[u]["level"], u) for u in uids if u in page and page[u]["href"])
        if not rows:
            return
        level, uid = rows[0]
        if reserved and reserved.get("slot") not in (None, slot):
            return  # saving up for something else in this village
        log.info("army: %supgrade %s in the %s to level %d", "[dry-run] " if self.dry_run else "",
                 units.name(uid), building, level + 1)
        if not self.dry_run:
            self.c.follow(page[uid]["href"])
        actions.append({"action": "upgrade", "name": f"{units.name(uid)} ({building})", "slot": slot,
                        "reason": f"level {level + 1}", "time": int(time.time())})

    def _celebrate(self, vid, state, plan, phase, actions):
        cu = plan["culture"]
        th = rules.find_slot(state, "Town Hall")
        if not cu["celebrations"] or th is None:
            return
        great = rules.building_level(state, "Town Hall") >= 10 and phase in ("growth", "relics", "wonder")
        cost = {"wood": 29700, "clay": 33250, "iron": 32000, "crop": 6700} if great else \
            {"wood": 6400, "clay": 6650, "iron": 5940, "crop": 1340}
        res = state["resources"]
        if any(res[k]["amount"] - cost[k] < cu["keep_share"] * res[k]["cap"] for k in cost):
            if not great:
                return
            great, cost = False, {"wood": 6400, "clay": 6650, "iron": 5940, "crop": 1340}
            if any(res[k]["amount"] - cost[k] < cu["keep_share"] * res[k]["cap"] for k in cost):
                return
        if self.c.celebration_running(th):
            return
        kind = 2 if great else 1
        log.info("army: %shold a %s celebration (culture points)", "[dry-run] " if self.dry_run else "",
                 "great" if great else "small")
        if not self.dry_run:
            self.c.celebrate(th, kind)
        actions.append({"action": "celebrate", "name": "great celebration" if great else "small celebration",
                        "slot": th, "reason": "culture points", "time": int(time.time())})

    # --- reports: what attackers sent, what our scouts saw --------------------------------------------

    def learn(self, own_names):
        """Read new reports: defence reports teach the attacker's force; scouting reports what a target holds."""
        seen = set(self.data["seen_reports"])
        for rep in self.c.reports():
            rid, topic = rep["id"], rep["topic"]
            if rid in seen:
                continue
            seen.add(rid)
            self.data["seen_reports"].append(rid)
            attacked = any(topic.endswith(" " + n) for n in own_names) and " attacks " in topic
            scouted = " scouts " in topic and any(topic.startswith(n + " ") for n in own_names)
            if not attacked and not scouted:
                continue
            d = self.c.report_detail(rid)
            if not d:
                continue
            if attacked and d.get("attacker_uid"):
                self.hostiles.note_force(d["attacker_uid"], d["sent"])
            if scouted and d.get("target"):
                key = str(d["target"])
                self.data["scouted"][key] = {"time": int(time.time()), "def_troops": d["def_troops"],
                                             "info": d.get("info", "")[:200]}
                self.data["scouting"].pop(key, None)
                asker = self.data.get("scout_requests", {}).pop(key, None)
                if asker and not self.dry_run:
                    self._report_scouting(asker, topic, d)

    def _report_scouting(self, asker, topic, d):
        """A member asked for this scouting run: tell them what the scouts saw."""
        if d["def_troops"] is None:
            seen = "my scouts did not come back, so the village must be well guarded"
        elif not d["def_troops"]:
            seen = "no troops at home"
        else:
            seen = "troops at home: " + ", ".join(f"{n} {units.name(u)}" for u, n in d["def_troops"].items())
        text = f"My scouts are back ({topic}): {seen}."
        if d.get("info"):
            text += f" They also saw: {d['info']}."
        try:
            self.c.send_message(asker, "Scouting report", text + f"\n\n{self.c.user}")
            self._note(f"told {asker} what the scouts saw ({topic})")
        except Exception as e:  # a letter must never stop the bot
            log.warning("could not write to %s: %s", asker, e)

    # --- counter-strikes ------------------------------------------------------------------------------

    def strikes(self, villages, roles, coords, plan, me, member_names, map_pop, war_champ=None):
        """Answer attacks on the alliance from the army villages. `map_pop`: wref -> population (raid book map).
        Returns actions."""
        cfg = plan["counter"]
        if not cfg["enabled"]:
            return []
        now = time.time()
        hostile = self.hostiles.active(cfg["hostile_days"], now)
        if not hostile:
            return []
        if len(self.hostiles.strikes_since(now - 86400, bot=me)) >= cfg["per_bot_per_day"]:
            return []
        actions = []
        scout = combat.SCOUTS.get(self.tribe)
        for v in villages:
            if roles.get(v["id"]) != "army":
                continue
            c = coords.get(v["id"])
            st = v["state"]
            if not c or st["incoming"]["attacks"] or not rules.find_slot(st, "Rally Point"):
                continue
            offence = {u: st["troops"].get(u, 0) for u in combat.offence_units(self.tribe)}
            offence = {u: n for u, n in offence.items() if n > 0}
            rams = st["troops"].get(combat.unit_id(self.tribe, 7), 0)
            targets = []
            for uid, p in hostile.items():
                for wref, xy in p["villages"].items():
                    if not xy or xy[0] is None:
                        continue
                    d = math.hypot(xy[0] - c[0], xy[1] - c[1])
                    if d <= cfg["radius"]:
                        targets.append((d, uid, p, int(wref), xy))
            for d, uid, p, wref, (x, y) in sorted(targets):
                key = str(wref)
                if now < self.data["skip"].get(key, 0):
                    continue
                if len(self.hostiles.strikes_since(now - 86400, uid=uid)) >= cfg["per_target_per_day"]:
                    continue
                pop = map_pop.get(wref)
                if pop is not None and pop < cfg["min_target_pop"]:
                    continue
                seen = self.data["scouted"].get(key)
                if not seen or now - seen["time"] > cfg["scout_max_age_h"] * 3600:
                    if now - self.data["scouting"].get(key, 0) < SCOUT_WAIT_S:
                        continue  # scouts on the way
                    if scout and st["troops"].get(scout, 0) >= cfg["scouts_per_run"]:
                        actions += self._send(v, x, y, 3, {combat.slot_of(scout): cfg["scouts_per_run"]},
                                              f"scout {p['name']}'s village ({x}|{y})", spy=1)
                        if not self.dry_run:
                            self.data["scouting"][key] = int(now)
                    continue
                if seen["def_troops"] is None:
                    self.data["skip"][key] = now + STRIKE_RETRY_S
                    continue
                if not offence:
                    break
                inf, cav = combat.attack_power(offence)
                need = cfg["margin"] * combat.defence_power(seen["def_troops"], inf / max(inf + cav, 1))
                theirs = 0
                if p.get("force"):
                    theirs = sum(combat.attack_power({int(u): n for u, n in p["force"].items()}))
                force = combat.size_force(offence, max(need, min(cfg["size_factor"] * theirs, inf + cav)))
                if force is None:
                    self._note(f"{p['name']}'s village ({x}|{y}) is too strong to strike now "
                               f"(defence ~{int(need / cfg['margin'])}, our attack {inf + cav})")
                    self.data["skip"][key] = now + STRIKE_RETRY_S
                    continue
                slots = {combat.slot_of(u): n for u, n in force.items()}
                if rams:
                    slots[7] = min(rams, max(1, sum(force.values()) // 15))
                champ = war_champ(v["id"]) if war_champ else None   # the war champion marches along when home
                acts = self._send(v, x, y, 3, slots, f"strike back at {p['name']}'s village ({x}|{y})"
                                  + (" with the war champion" if champ else ""), champ_id=champ)
                if acts:
                    actions += acts
                    if not self.dry_run:
                        self.hostiles.note_strike(uid, me, wref, force, now)
                    for u, n in force.items():
                        offence[u] -= n
                    offence = {u: n for u, n in offence.items() if n > 0}
                    self._tell_victims(p, me, member_names)
                else:
                    self.data["skip"][key] = now + 86400
                if len(self.hostiles.strikes_since(now - 86400, bot=me)) >= cfg["per_bot_per_day"]:
                    return actions
        return actions

    def _send(self, v, x, y, c, slots, what, spy=None, ctar1=None, champion=False, champ_id=None):
        log.info("[%s] army: %s%s with %s", v["name"], "[dry-run] " if self.dry_run else "", what,
                 ", ".join(f"{n} {units.name(combat.unit_id(self.tribe, s))}" for s, n in slots.items()))
        if self.dry_run:
            return [{"action": "attack" if c == 3 else "send", "name": what, "slot": 39, "village": v["name"],
                     "reason": "army", "time": int(time.time())}]
        if v["id"] is not None:
            self.c.select_village(v["id"])
        r = self.c.send_army(x, y, c, slots, champion=champion, ctar1=ctar1, spy=spy, champ_id=champ_id)
        if not r["ok"]:
            self._note(f"the game refused: {what} ({r['error']})")
            return []
        self._note(what)
        return [{"action": "attack" if c == 3 else "send", "name": what, "slot": 39, "village": v["name"],
                 "reason": "army", "time": int(time.time())}]

    def _tell_victims(self, p, me, member_names):
        """A short note to the human members this player attacked: the alliance answered."""
        for victim in p.get("victims", {}):
            if victim in member_names and victim != me and not self.dry_run:
                try:
                    self.c.send_message(victim, f"We answered {p['name']}"[:35],
                                        f"{p['name']} attacked you. My army is on its way to them now - "
                                        f"they will think twice next time.\n\n{me}")
                except Exception as e:  # a note must never stop the bot
                    log.warning("could not write to %s: %s", victim, e)

    # --- timed sends (requests from letters) -----------------------------------------------------------

    def schedule(self, entry):
        """entry: {"depart", "vid", "vname", "x", "y", "c", "slots", "what", "for"} (slots: {form slot: n})."""
        self.data["schedule"].append(entry)

    def next_departure(self):
        return min((e["depart"] for e in self.data["schedule"]), default=None)

    def run_schedule(self):
        now, actions, keep = time.time(), [], []
        for e in self.data["schedule"]:
            if e["depart"] > now + 20:
                keep.append(e)
                continue
            if now - e["depart"] > 600:
                self._note(f"missed the departure for {e['what']} (bot was busy)")
                continue
            v = {"id": e["vid"], "name": e["vname"]}
            actions += self._send(v, e["x"], e["y"], e["c"], {int(k): n for k, n in e["slots"].items()}, e["what"],
                                  champion=e.get("champion", False), ctar1=e.get("ctar1"))
        self.data["schedule"] = keep
        return actions

    def summary(self):
        return {"scouted": len(self.data["scouted"]), "scheduled": len(self.data["schedule"]),
                "log": self.data["log"][-8:]}
