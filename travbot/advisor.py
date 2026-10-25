"""AI advisor: when the PC is idle, ask a local Ollama model for a new plan.

Everything here is best-effort. Any failure (PC off, user busy, model error, bad JSON)
just means no new plan this time; the rules engine keeps using the last plan or the default.
"""
import json
import logging
import time

import requests

from . import plan as planmod

log = logging.getLogger(__name__)

# Buildings the advisor may target. Only buildings the bot can make use of: economy, storage,
# defence (Barracks for the basic defender, Palisade) and expansion (Residence).
BUILDING_NAMES = [
    "Main Building", "Warehouse", "Granary", "Cranny", "Rally Point", "Marketplace",
    "Embassy", "Grain Mill", "Bakery", "Sawmill", "Brickyard", "Iron Foundry",
    "Residence", "Palisade", "Barracks",
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
                "properties": {"name": {"type": "string", "enum": BUILDING_NAMES},
                               "level": {"type": "integer"}},
                "required": ["name", "level"],
            },
        },
        "defense": {
            "type": "object",
            "properties": {"unit_target": {"type": "integer"}, "budget_share": {"type": "number"},
                           "emergency_share": {"type": "number"}, "reinforce": {"type": "boolean"},
                           "reinforce_keep": {"type": "integer"}, "reinforce_share": {"type": "number"},
                           "reinforce_radius": {"type": "integer"}},
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
            "properties": {"finish_now": {"type": "boolean"}, "min_minutes_left": {"type": "integer"},
                           "reserve": {"type": "integer"}, "capital_reserve": {"type": "integer"},
                           "max_per_tick": {"type": "integer"}},
            "required": ["finish_now", "min_minutes_left", "reserve", "capital_reserve", "max_per_tick"],
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
                 "save_up_hours", "building_targets", "defense", "expansion", "gold", "share"],
}

SYSTEM_PROMPT = """You are the strategy advisor for a bot playing Empires of Rome (a Roman-era browser strategy game), tribe Gauls.
The bot upgrades resource fields, constructs/upgrades buildings, trains the basic defensive unit (Phalanx) in
the Barracks, founds new villages with settlers, spends village gold on "Finish now" and ships surplus resources
from rich villages to young ones through the Marketplace. It does not attack or research. The same plan is
applied to every village the bot owns.

You do not act directly. You write a plan that a rules engine follows until your next review (about an hour):
- resource_weights (0-10 each): higher = the bot upgrades that resource's fields sooner. The bot picks the
  resource with the lowest production divided by its weight.
- field_max_level (1-20): fields are not upgraded past this level (every village on this server can reach 20;
  levels above 10 get expensive quickly).
- min_net_crop: if net crop per hour (after upkeep) drops below this, the bot upgrades cropland first.
- storage_buffer_hours: the bot grows the Warehouse/Granary when a resource would fill up within this many hours.
- save_up_hours (0-24): the bot saves resources for its top-priority item if that becomes affordable within this
  many hours, instead of spending on cheaper items. 0 disables saving up.
- building_targets: ordered list of {name, level}; the bot works on the first unfinished targets in order.
  List the full path you want, at most 15 entries, levels 1-20.
- defense.unit_target: Phalanxes to keep in each village (0-1000). Each costs 100 wood, 130 clay, 55 iron,
  30 crop and 1 crop/h upkeep. defense.budget_share (0-0.9): share of resources the bot may spend on them per
  round; defense.emergency_share (0-1): share it may spend while an attack is incoming.
  defense.reinforce (true/false): villages within reinforce_radius tiles (1-50) send reinforcements to own or
  allied villages with an incoming attack: reinforce_share (0-1) of their defenders above reinforce_keep (always
  stay home); villages that are attacked themselves never send.
- expansion: enabled (true/false); max_villages; search_radius (tiles, 2-15) for free valleys; min_crop_fields
  (6 = normal valley); ally_buffer (0-10): skip valleys this close to an allied bot's village or settling
  target. Founding needs Residence 10, 3 settlers (expensive), culture points and a Rally Point.
- gold: finish_now (true/false) spends 50 village gold to finish a running build job at once, longest jobs first,
  only jobs with at least min_minutes_left (when the village's gold is nearly full: 5 min); every village keeps
  `reserve` gold, the capital keeps `capital_reserve` (the capital pays the weekly Plus + production bonus
  renewals, about 5,500 gold a week); max_per_tick (0-10) finishes per village per round.
- share: enabled (true/false); a village producing less than young_ratio (0-1) of the richest village is "young"
  and gets resources from the richest villages that have a Marketplace, up to fill_to (0.1-1) of its storage;
  a donor never drops below donor_keep_share (0-0.95) of its storage; shipments under min_shipment are skipped.
  share.allies (true/false) also ships to young villages of the allied bots (same alliance), the donor keeping
  at least ally_keep_share (0-0.95) of its storage. Merchants (one per Marketplace level) each carry a lot on this server, so more Marketplace levels = bigger shipments.

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
Gold: each village produces gold (more with Main Building and Marketplace levels). Raids can steal gold that
is not protected by a Treasury (needs Main Building 10; it holds 1/10 of a Warehouse of the same level). The bot
spends gold on Finish now (see "gold"); a village's gold stops growing at its cap (8,000 without a Treasury).
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
    ])


def _summary(status, current_plan):
    lines = [f"The bot owns {len(status['villages'])} village(s)."]
    lines += [_village_summary(v) for v in status["villages"]]
    lines.append("Expansion progress: " + (json.dumps(status.get("expansion")) if status.get("expansion") else "not started"))
    lines.append("Recent bot actions: " + (", ".join(
        f"{a['action']} {a['name']}" for a in status["recent_actions"][-12:]) or "none"))
    lines.append("Current plan: " + json.dumps(current_plan))
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
    def __init__(self, ollama_url, idle_url, models, state_dir, interval_s, retry_s, timeout_s=600, tribe=3):
        self.ollama = ollama_url.rstrip("/")
        self.idle_url = idle_url
        self.models = models
        self.state_file = state_dir / "advisor.json"
        self.plan_file = state_dir / "plan.json"
        self.interval_s = interval_s
        self.retry_s = retry_s
        self.timeout_s = timeout_s
        self.tribe = tribe

    def _load_state(self):
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"last_attempt": 0, "last_success": 0}

    def _save_state(self, st):
        self.state_file.write_text(json.dumps(st, indent=2), encoding="utf-8")

    def pc_idle(self):
        """(idle?, reason). An unreachable PC counts as not idle."""
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
        if others:
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
            "options": {"temperature": 0.3, "num_ctx": 8192},
            "messages": [{"role": "system", "content": system_prompt(self.tribe)},
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
                    "options": {"temperature": 0.8, "num_ctx": 2048},
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
            clean.update(created=int(time.time()), source=f"llm:{model}", notes=str(raw.get("notes", ""))[:500])
            tmp = self.plan_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(clean, indent=2), encoding="utf-8")
            tmp.replace(self.plan_file)
            st["last_success"] = time.time()
            st["last_model"] = model
            self._save_state(st)
            return f"advisor: new plan from {model} in {time.time() - started:.0f}s: {clean['notes']}"
        return "advisor: all models failed, keeping current plan"
