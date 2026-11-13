"""AI advisor: when the PC is idle, ask a local Ollama model for a new plan.

Everything here is best-effort. Any failure (PC off, user busy, model error, bad JSON)
just means no new plan this time; the rules engine keeps using the last plan or the default.
"""
import json
import random
import logging
import time

import requests

from . import plan as planmod
from .names import display, display_text

log = logging.getLogger(__name__)

# Buildings the advisor may target: economy, storage, defence, expansion, and (2026-11-13) the army,
# culture and support buildings the bot now uses. Village roles (travbot/roles.py) add their own on top.
BUILDING_NAMES = [
    "Main Building", "Warehouse", "Granary", "Cranny", "Rally Point", "Marketplace",
    "Embassy", "Grain Mill", "Bakery", "Sawmill", "Brickyard", "Iron Foundry",
    "Residence", "Palisade", "Barracks", "Academy", "Blacksmith", "Armoury", "Stable", "Workshop",
    "Town Hall", "Hero's Mansion", "Trade Office", "Great Warehouse", "Great Granary", "Treasury",
]

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "notes": {"type": "string"},
        "resource_weights": {
            "type": "object",
            "properties": {k: {"type": "number"} for k in planmod.RESOURCES},
            "required": list(planmod.RESOURCES),
        },
        "field_max_level": {"type": "integer"},
        "min_net_crop": {"type": "integer"},
        "storage_buffer_hours": {"type": "number"},
        "save_up_hours": {"type": "number"},
        "building_targets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {"name": {"type": "string", "enum": [display(n) for n in BUILDING_NAMES]},
                               "level": {"type": "integer"}},
                "required": ["name", "level"],
            },
        },
        "defense": {
            "type": "object",
            "properties": {"unit_target": {"type": "integer"}, "budget_share": {"type": "number"},
                           "emergency_share": {"type": "number"}, "reinforce": {"type": "boolean"},
                           "reinforce_keep": {"type": "integer"}, "reinforce_share": {"type": "number"},
                           "reinforce_radius": {"type": "integer", "minimum": 25, "maximum": 50}},
            "required": ["unit_target", "budget_share", "emergency_share"],
        },
        "expansion": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}, "max_villages": {"type": "integer"},
                           "search_radius": {"type": "integer"}, "min_crop_fields": {"type": "integer"},
                           "ally_buffer": {"type": "integer"}},
            "required": ["enabled", "max_villages", "search_radius", "min_crop_fields"],
        },
        "gold": {
            "type": "object",
            "properties": {"finish_now": {"type": "boolean"}, "max_finish_price": {"type": "integer"},
                           "min_minutes_per_gold": {"type": "number"},
                           "reserve": {"type": "integer"}, "capital_reserve": {"type": "integer"},
                           "max_per_tick": {"type": "integer"}},
            "required": ["finish_now", "max_finish_price", "min_minutes_per_gold", "reserve", "capital_reserve",
                         "max_per_tick"],
        },
        "army": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}, "unit_target": {"type": "integer"},
                           "budget_share": {"type": "number"}, "cavalry_share": {"type": "number"}},
            "required": ["enabled", "unit_target", "budget_share", "cavalry_share"],
        },
        "culture": {
            "type": "object",
            "properties": {"celebrations": {"type": "boolean"}, "keep_share": {"type": "number"}},
            "required": ["celebrations", "keep_share"],
        },
        "wonder": {
            "type": "object",
            "properties": {"keep_share": {"type": "number"}, "troop_share": {"type": "number"}},
            "required": ["keep_share", "troop_share"],
        },
        "share": {
            "type": "object",
            "properties": {"enabled": {"type": "boolean"}, "young_ratio": {"type": "number"},
                           "donor_keep_share": {"type": "number"}, "fill_to": {"type": "number"},
                           "min_shipment": {"type": "integer"}, "allies": {"type": "boolean"},
                           "ally_keep_share": {"type": "number"}},
            "required": ["enabled", "young_ratio", "donor_keep_share", "fill_to", "min_shipment"],
        },
    },
    "required": ["notes", "resource_weights", "field_max_level", "min_net_crop", "storage_buffer_hours",
                 "save_up_hours", "building_targets", "defense", "expansion", "gold", "share", "army", "culture",
                 "wonder"],
}

SYSTEM_PROMPT = """You are the strategy advisor for a bot playing Empires of Rome (a Roman-era browser strategy game), tribe Gauls.
The bot is an AI ally in an alliance with human players. Its job is to help its alliance win and to be a
strong, credible player that is not worth farming - never a bully: it never raids active players.

How this world is won (the playbook): the alliance that finishes a Wonder of the Ancients at level 100 wins. The
HUMAN members build the Wonder; the bot's part is a strong economy, many villages, an army that has the
alliance's back, and sending resources and troops to the alliance's Wonder and relic villages.
Phases by world day: opening (1-4: balanced fields to ~5, Main Building, Cranny, Embassy), second village (5-9:
Residence 10, settlers, culture points), growth (10-29: settle whenever culture points allow, capital becomes a
cropper, an army village with Barracks/Stable/Academy/Blacksmith/Workshop, Town Hall celebrations), relics
(30-49: relics appear in Etruscan villages; bigger army with catapults, defend relic holders), wonder (50+:
Wonder villages appear; feed the alliance's Wonder, stack defence in it; the Etruscans attack Wonders from day 60).

What the bot does on its own (you don't control these directly):
- village roles: the capital (crop, Town Hall, Armoury), one army village (two from 8 villages) that trains the
  attack army and upgrades it in the Blacksmith, and feeder villages; each role gets the buildings it needs for
  the phase in front of your building_targets;
- it researches its attack units, rams and catapults, upgrades attack (Blacksmith) and defence (Armoury);
- it keeps a hostile list of players who attacked it or an alliance member, scouts them and strikes back in
  proportion (never small or protected players, never catapults or conquest against humans);
- it answers members' letters, sending resources, defenders, scouts or an attack they ask for, within limits;
- it makes up to three champions (1 with a Champion's Hall, 2 at Hall 10, 3 at Hall 20): a war champion that
  marches with counter-strikes and clears oases, a steward in the capital (resources and gold) and a guardian
  in the most-attacked village (defence). The roles build the Halls.
Your plan applies to every village; the roles add on top.

You do not act directly. You write a plan that a rules engine follows until your next review (about an hour):
- resource_weights (0-10 each): higher = the bot upgrades that resource's fields sooner. The bot picks the
  resource with the lowest production divided by its weight.
- field_max_level (1-25): fields are not upgraded past this level (every village on this server can reach 25;
  levels above 10 get expensive quickly). A village that has reached it, has no building targets left and is
  more than half full keeps upgrading fields anyway, so a low value never leaves resources unused.
- min_net_crop: if net crop per hour (after upkeep) drops below this, the bot upgrades cropland first.
- storage_buffer_hours: the bot grows the Warehouse/Granary when a resource would fill up within this many hours.
- save_up_hours (0-24): the bot saves resources for its top-priority item if that becomes affordable within this
  many hours, instead of spending on cheaper items. 0 disables saving up.
- building_targets: ordered list of {name, level}; the bot works on the first unfinished targets in order.
  List the full path you want, at most 15 entries, levels 1-20.
- defense.unit_target: Phalanxes to keep in each village (0-1000). Each costs 100 wood, 130 clay, 55 iron,
  30 crop and 1 crop/h upkeep. defense.budget_share (0-0.9): share of resources the bot may spend on them per
  round; defense.emergency_share (0-1): share it may spend while an attack is incoming.
  defense.reinforce (true/false): villages within reinforce_radius tiles (25-50; keep it wide - new players spawn
  further from the centre as the world ages) send reinforcements to own or
  allied villages with an incoming attack: reinforce_share (0-1) of their defenders above reinforce_keep (always
  stay home); villages that are attacked themselves never send.
- expansion: enabled (true/false); max_villages; search_radius (tiles, 2-15) for free valleys; min_crop_fields
  (6 = normal valley); ally_buffer (0-10): skip valleys this close to an allied bot's village or settling
  target. Founding needs Residence 10, 3 settlers (expensive), culture points and a Rally Point.
- gold: finish_now (true/false) spends village gold to finish a running build job at once. The price depends on
  the time left (10 gold for a few minutes, 25 for 1 h, 100 for 8 h, 200 for a day, 700 for a week), so only good
  value is bought: at most max_finish_price (10-1000) per job and at least min_minutes_per_gold (0-60) minutes saved
  per gold, best value first (gold nearly full: price limit x2, value bar /2); every village keeps
  `reserve` gold, the capital keeps `capital_reserve` (the capital pays the weekly Plus + production bonus
  renewals, about 5,500 gold a week); max_per_tick (0-10) finishes per village per round.
- share: enabled (true/false); a village producing less than young_ratio (0-1) of the richest village is "young"
  and gets resources from the richest villages that have a Marketplace, up to fill_to (0.1-1) of its storage;
  a donor never drops below donor_keep_share (0-0.95) of its storage; shipments under min_shipment are skipped.
  share.allies (true/false) also ships to young villages of the allied bots (same alliance), the donor keeping
  at least ally_keep_share (0-0.95) of its storage. Merchants (one per Marketplace level) each carry a lot on this server, so more Marketplace levels = bigger shipments.
  Marketplace levels do nothing else (no production, no gold).

Maximum levels: Sawmill, Brickyard, Iron Foundry, Grain Mill and Bakery stop at level 5 (each adds 5% per
level, 25% at most). Every other building in the list goes to 20.

Construction requirements (a building cannot be started before these are met; the bot skips such targets):
- Warehouse, Granary, Embassy: Main Building 1. Cranny, Rally Point, Palisade: none.
- Barracks: Main Building 3, Rally Point 1. Marketplace: Main Building 3, Warehouse 1, Granary 1.
- Residence: Main Building 5. Grain Mill: a Cropland at level 5.
- Bakery: Main Building 5, a Cropland at level 10, Grain Mill 5.
- Sawmill / Brickyard / Iron Foundry: Main Building 5 and a Woodcutter / Clay Pit / Iron Mine at level 10.
Only include a target when its requirements are met now or will be met by earlier targets/fields soon.

Guidance: balanced fields early; keep net crop comfortably positive because buildings, fields and troops add
upkeep; Main Building levels speed up all construction; a Cranny hides resources from raids; keep a modest
defence early and raise it if attacks were seen; expansion is a long-term goal once the economy is solid
and is limited by culture points (every building level adds some).
A Treasury (Main Building 10) hides village gold from raiders: the roles give the capital Treasury 10 and the
other villages Treasury 5 later; raise it only where a lot of gold sits. Bots never use it for relics.
Gold: only Gold Mines produce gold (resource fields 45/46 in some villages; the bot levels them with the
fields). Main Building and Marketplace levels do not add gold. Raids can steal gold that
is not protected by a Treasury (needs Main Building 10; it holds 1/10 of a Warehouse of the same level). The bot
spends gold on Finish now (see "gold"); a village's gold stops growing at its cap (8,000 without a Treasury).
- army: enabled; unit_target (0-3000): attack units in the growth phase (x2 in relics, x3 in wonder), split over
  the army villages; the bot never keeps more than 250 per village it owns, so aim for about 100-150 per village
  - the economy comes first, troops cost upkeep; budget_share (0-0.9) of resources per round for them; cavalry_share (0-1) of the army that is
  cavalry (faster, better raiders; infantry is cheaper). Raise it when the alliance is attacked a lot.
- culture: celebrations (true/false) in Town Halls for culture points (more villages); keep_share (0-0.9) of
  storage a village keeps when it starts one.
- wonder: keep_share (0.1-0.9) of storage each village keeps when shipping to the alliance's Wonder;
  troop_share (0-0.8) of spare defenders sent to stack in it (only matters once a member holds a Wonder).
Put a short explanation (max 3 sentences) in "notes"."""


def _village_summary(v):
    res = v["resources"]
    fields = {}
    for f in v["fields"]:
        fields.setdefault(f["kind"], []).append(f["level"])
    inc = v["incoming"]
    return "\n".join([
        f"Village {v['name']}:",
        "  Resources (amount/capacity, production per hour): " + "; ".join(
            f"{k} {res[k]['amount']}/{res[k]['cap']} {res[k]['prod']}/h" for k in planmod.RESOURCES)
        + " (crop production is net, after upkeep)",
        "  Field levels: " + "; ".join(f"{k} {sorted(l)}" for k, l in sorted(fields.items())),
        "  Buildings: " + (", ".join(f"{b['name']} {b['level']}" for b in v["buildings"].values()) or "none"),
        "  Build queue: " + (", ".join(f"{q['name']} -> {q['to_level']} ({q['seconds_left'] // 60} min)"
                                       for q in v["queue"]) or "empty"),
        "  Troops: " + (", ".join(f"{n} {name}" for name, n in v["troops"].items()) or "none"),
        f"  Incoming attacks: {inc['attacks']}" + (f", first in {(inc['first_in'] or 0) // 60} min" if inc["attacks"] else ""),
        "  Saving for: " + (f"{v['saving_for']['name']} (~{v['saving_for']['eta_min']} min)" if v.get("saving_for") else "nothing"),
        "  Gold: " + (f"{v['gold']['amount']}/{v['gold']['cap']} (+{v['gold']['prod']}/h)" if v.get("gold") else "unknown")
        + (" - this is the capital" if v.get("capital") else ""),
        f"  Role: {v.get('role', 'feeder')}",
    ])


def _summary(status, current_plan):
    w = status.get("world") or {}
    lines = [f"World day {w.get('day') or 'unknown'}, phase: {w.get('phase', 'growth')} (relics day "
             f"{w.get('relics_day')}, Wonder villages day {w.get('wonder_day')}).",
             f"The bot owns {len(status['villages'])} village(s)."]
    lines += [_village_summary(v) for v in status["villages"]]
    lines.append("Expansion progress: " + (json.dumps(status.get("expansion")) if status.get("expansion") else "not started"))
    lines.append("Recent bot actions: " + (", ".join(
        f"{a['action']} {a['name']}" for a in status["recent_actions"][-12:]) or "none"))
    hostile = status.get("hostiles") or []
    lines.append("Players who attacked the alliance recently: " + ("; ".join(hostile) if hostile else "none"))
    ally = status.get("alliance") or {}
    if ally:
        lines.append(f"Alliance: {ally.get('members', 0)} members ({ally.get('humans', 0)} human); "
                     f"Wonder villages: {ally.get('wonders', 0)}; relic villages: {ally.get('relics', 0)}.")
    army = status.get("army") or {}
    if army.get("log"):
        lines.append("Recent army events: " + "; ".join(e["text"] for e in army["log"][-5:]))
    lines.append("Current plan: " + json.dumps({k: v for k, v in current_plan.items() if k not in ("counter",)}))
    return "\n".join(lines)


TRIBE_NAMES = {1: "Romans", 2: "Teutons", 3: "Gauls"}


def system_prompt(tribe):
    """The prompt above, for the bot's own tribe (it was written for the Gauls)."""
    if tribe == 3:
        return SYSTEM_PROMPT
    from . import units, rules
    unit = units.name(rules.basic_defender(tribe))
    out = SYSTEM_PROMPT.replace("tribe Gauls", "tribe " + TRIBE_NAMES.get(tribe, f"{tribe}"))
    out = out.replace("Each costs 100 wood, 130 clay, 55 iron,\n  30 crop and 1 crop/h upkeep.",
                      "Each costs resources and 1 crop/h upkeep.")
    out = out.replace("Phalanxes", units.plural(unit)).replace("Phalanx", unit)
    out = out.replace("Palisade", {1: "City Wall", 2: "Earth Wall"}.get(tribe, "Palisade"))
    return out


LETTER_SCHEMA = {
    "type": "object",
    "properties": {"subject": {"type": "string"}, "text": {"type": "string"}},
    "required": ["subject", "text"],
}


class Advisor:
    def __init__(self, ollama_url, idle_url, models, state_dir, interval_s, retry_s, timeout_s=600, tribe=3,
                 yield_to_others=True):
        self.ollama = ollama_url.rstrip("/")
        self.idle_url = idle_url
        self.models = models
        self.state_file = state_dir / "advisor.json"
        self.plan_file = state_dir / "plan.json"
        self.interval_s = interval_s
        self.retry_s = retry_s
        self.timeout_s = timeout_s
        self.tribe = tribe
        self.yield_to_others = yield_to_others  # False = shared Ollama server: ask anyway, Ollama queues/swaps models

    def _load_state(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"last_attempt": 0, "last_success": 0}

    def _save_state(self, st):
        self.state_file.write_text(json.dumps(st, indent=2), encoding="utf-8")

    def pc_idle(self):
        """(idle?, reason). An unreachable PC counts as not idle. No idle URL (TRAVBOT_IDLE_URL="") = a dedicated
        Ollama server that is always available; only the "another model is loaded" check applies."""
        if self.idle_url:
            try:
                info = requests.get(self.idle_url, timeout=5).json()
            except (requests.RequestException, ValueError) as e:
                return False, f"idle check unreachable ({type(e).__name__})"
            if not info.get("idle"):
                return False, "PC busy: " + ", ".join(info.get("busy_reasons") or ["unknown"])
        try:
            loaded = [m["name"] for m in requests.get(f"{self.ollama}/api/ps", timeout=5).json().get("models", [])]
        except (requests.RequestException, ValueError) as e:
            return False, f"Ollama unreachable ({type(e).__name__})"
        others = [m for m in loaded if m not in self.models]
        if others and self.yield_to_others:
            # someone (e.g. the work laptop) used another model in the last few minutes
            return False, "Ollama in use: " + ", ".join(others)
        return True, "idle"

    def _ask(self, model, status, current_plan):
        r = requests.post(f"{self.ollama}/api/chat", timeout=self.timeout_s, json={
            "model": model,
            "stream": False,
            "think": False,
            "format": PLAN_SCHEMA,
            "keep_alive": 0,  # unload right after answering: give the GPU straight back
            "options": {"temperature": 0.3, "num_ctx": 8192, "num_predict": 3000},  # cap: small models can run away on a strict format
            "messages": [{"role": "system", "content": display_text(system_prompt(self.tribe))},
                         {"role": "user", "content": _summary(status, current_plan)}],
        })
        r.raise_for_status()
        return json.loads(r.json()["message"]["content"])

    def letter(self, kind, ctx):
        """An in-game letter written by the local model: (subject, text), or None if the PC is busy/off."""
        idle, _why = self.pc_idle()
        if not idle:
            return None
        who = TRIBE_NAMES.get(self.tribe, "a tribe")
        if kind == "invite":
            task = (f"You are {ctx['me']}, a chieftain of the {who} in Empires of Rome, a Roman-era strategy game. You have founded the "
                    f"alliance \"{ctx['alliance']}\" [{ctx['tag']}] and just invited your neighbour {ctx['to']} to join "
                    f"(the invitation waits at their Embassy). Write them a short, warm, in-character letter asking "
                    f"them to join: mutual protection, shared surplus, growing together.")
        elif kind == "accept":
            task = (f"You are {ctx['me']}, a chieftain of the {who} in Empires of Rome, a Roman-era strategy game. You just accepted "
                    f"{ctx['to']}'s invitation and joined the alliance \"{ctx['alliance']}\". Write {ctx['to']} a short, "
                    f"in-character letter of thanks and loyalty.")
        else:
            return None
        task += " Sign with your name. Keep the text under 600 characters and the subject under 50."
        for model in self.models:
            try:
                r = requests.post(f"{self.ollama}/api/chat", timeout=180, json={
                    "model": model, "stream": False, "think": False, "format": LETTER_SCHEMA, "keep_alive": 0,
                    "options": {"temperature": 0.8, "num_ctx": 2048, "num_predict": 600},
                    "messages": [{"role": "user", "content": task}]})
                r.raise_for_status()
                out = json.loads(r.json()["message"]["content"])
                subject, text = str(out["subject"]).strip()[:50], str(out["text"]).strip()[:1500]
                if subject and text:
                    return subject, text
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("letter by %s failed: %s", model, e)
        return None

    def maybe_run(self, status, current_plan, force=False):
        """Run a review if one is due and the PC is idle. Returns a short log string."""
        # a hand-written plan ("source": "manual...") is never replaced by the model; the model then only writes letters
        if not force and ("(manual" in str((status or {}).get("plan_source", ""))
                          or str((current_plan or {}).get("source", "")).startswith("manual")):
            return None
        st = self._load_state()
        now = time.time()
        if not force:
            if now - st["last_success"] < self.interval_s:
                return None
            if now - st["last_attempt"] < self.retry_s:
                return None
        st["last_attempt"] = now
        self._save_state(st)

        idle, why = (True, "forced") if force else self.pc_idle()
        if not idle:
            return f"advisor skipped: {why}"

        for model in self.models:
            started = time.time()
            try:
                raw = self._ask(model, status, current_plan)
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("advisor model %s failed: %s", model, e)
                continue
            clean = planmod.validate(raw)
            # the never-raid list can only grow: a model once dropped the bots' own names from it
            old_ex = list(((current_plan or {}).get("raid") or {}).get("exclude") or [])
            new_ex = list((clean.get("raid") or {}).get("exclude") or [])
            if "raid" in clean:
                clean["raid"]["exclude"] = new_ex + [x for x in old_ex if x not in new_ex]
            clean.update(created=int(time.time()), source=f"llm:{model}", notes=str(raw.get("notes", ""))[:500])
            tmp = self.plan_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
            tmp.replace(self.plan_file)
            # a little random spread so bots restarted together don't all ask the model at the same moment again
            st["last_success"] = time.time() - random.uniform(0, 0.25 * self.interval_s)
            st["last_model"] = model
            self._save_state(st)
            return f"advisor: new plan from {model} in {time.time() - started:.0f}s: {clean['notes']}"
        return "advisor: all models failed, keeping current plan"
