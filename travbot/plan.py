"""The strategy plan: what the rules engine aims for.

The built-in DEFAULT_PLAN is always available. A plan file (written by the AI advisor,
or by hand) overrides it while it is fresh; stale, missing or invalid files fall back to
the default, so the bot never depends on the advisor being online.
"""
import json
import logging
import time

log = logging.getLogger(__name__)

DEFAULT_PLAN = {
    # relative priority of each resource when choosing which field to upgrade
    "resource_weights": {"wood": 1.0, "clay": 1.0, "iron": 1.0, "crop": 0.8},
    "field_max_level": 10,
    # keep net crop (production after upkeep) at least this much per hour
    "min_net_crop": 5,
    # grow warehouse/granary when a resource would fill up within this many hours
    "storage_buffer_hours": 4,
    # save resources for the top-priority item if it is affordable within this many hours
    # (cheaper items may only go ahead if they delay it by at most ~10 minutes)
    "save_up_hours": 2,
    # village buildings, in priority order: reach each level before moving on
    "building_targets": [
        {"name": "Main Building", "level": 3},
        {"name": "Cranny", "level": 3},
        {"name": "Warehouse", "level": 3},
        {"name": "Granary", "level": 3},
        {"name": "Main Building", "level": 5},
        {"name": "Rally Point", "level": 1},
        {"name": "Barracks", "level": 1},
        {"name": "Marketplace", "level": 1},
        {"name": "Palisade", "level": 3},
        {"name": "Warehouse", "level": 7},
        {"name": "Granary", "level": 6},
        {"name": "Main Building", "level": 10},
        {"name": "Residence", "level": 10},
        {"name": "Cranny", "level": 6},
    ],
    "defense": {
        # basic defensive unit (no research needed) to keep in each village
        "unit_target": 20,
        # share of current resources the bot may spend on troops per tick
        "budget_share": 0.25,
        # share it may spend while an attack is incoming
        "emergency_share": 0.8,
        # send defenders to own/allied villages with incoming attacks (reinforcement, never attack)
        "reinforce": True,
        # defenders that always stay home in the sending village
        "reinforce_keep": 20,
        # share of the defenders above reinforce_keep that may go, per attacked village
        "reinforce_share": 0.5,
        # only villages within this many tiles of the attacked one send help
        "reinforce_radius": 15,
    },
    "expansion": {
        "enabled": True,
        "max_villages": 3,
        # how far (tiles) to look for a free valley around the settling village
        "search_radius": 8,
        # prefer valleys with at least this many croplands (6 = normal 4-4-4-6)
        "min_crop_fields": 6,
        # skip valleys within this many tiles of an allied bot's village or settling target
        "ally_buffer": 3,
    },
    "gold": {
        # spend village gold on "Finish now" (50 gold per running job)
        "finish_now": True,
        # only jobs with at least this much time left (when gold is nearly full: 5 min)
        "min_minutes_left": 30,
        # gold every village keeps; the capital keeps more for the weekly Plus/bonus renewals
        "reserve": 200,
        "capital_reserve": 6000,
        "max_per_tick": 2,
    },
    "share": {
        # rich villages ship surplus resources to young ones through the Marketplace
        "enabled": True,
        # a village producing less than this share of the richest one counts as young
        "young_ratio": 0.35,
        # the donor never goes below this share of its storage in any resource
        "donor_keep_share": 0.3,
        # fill the young village's storage up to this share
        "fill_to": 0.85,
        # don't bother with smaller shipments
        "min_shipment": 1000,
        # also ship to young villages of the allied bots (same alliance)
        "allies": True,
        # for shipments to allies the donor keeps at least this share of its storage
        "ally_keep_share": 0.3,
    },
}

RESOURCES = ("wood", "clay", "iron", "crop")


def _num(raw, key, lo, hi, cast=float):
    v = raw.get(key) if isinstance(raw, dict) else None
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not lo <= v <= hi:
        return None
    return cast(v)


def validate(raw):
    """Merge a raw plan over the default, keeping only well-formed values."""
    plan = json.loads(json.dumps(DEFAULT_PLAN))  # deep copy
    if not isinstance(raw, dict):
        return plan

    weights = raw.get("resource_weights")
    if isinstance(weights, dict):
        for k in RESOURCES:
            v = _num(weights, k, 0, 10)
            if v is not None:
                plan["resource_weights"][k] = v

    for key, lo, hi, cast in (("field_max_level", 1, 20, int), ("min_net_crop", 0, 1000, int),
                              ("storage_buffer_hours", 0.5, 48, float), ("save_up_hours", 0, 24, float)):
        v = _num(raw, key, lo, hi, cast)
        if v is not None:
            plan[key] = v

    targets = raw.get("building_targets")
    if isinstance(targets, list):
        clean = [{"name": t["name"].strip(), "level": int(t["level"])}
                 for t in targets
                 if isinstance(t, dict) and isinstance(t.get("name"), str) and t["name"].strip()
                 and isinstance(t.get("level"), int) and not isinstance(t.get("level"), bool)
                 and 1 <= t["level"] <= 20]
        if clean:
            plan["building_targets"] = clean[:40]

    defense = raw.get("defense")
    if isinstance(defense, dict):
        if isinstance(defense.get("reinforce"), bool):
            plan["defense"]["reinforce"] = defense["reinforce"]
        for key, lo, hi, cast in (("unit_target", 0, 1000, int), ("budget_share", 0, 0.9, float),
                                  ("emergency_share", 0, 1, float), ("reinforce_keep", 0, 1000, int),
                                  ("reinforce_share", 0, 1, float), ("reinforce_radius", 1, 50, int)):
            v = _num(defense, key, lo, hi, cast)
            if v is not None:
                plan["defense"][key] = v

    expansion = raw.get("expansion")
    if isinstance(expansion, dict):
        if isinstance(expansion.get("enabled"), bool):
            plan["expansion"]["enabled"] = expansion["enabled"]
        for key, lo, hi in (("max_villages", 1, 20), ("search_radius", 2, 15), ("min_crop_fields", 1, 15),
                             ("ally_buffer", 0, 10)):
            v = _num(expansion, key, lo, hi, int)
            if v is not None:
                plan["expansion"][key] = v

    gold = raw.get("gold")
    if isinstance(gold, dict):
        if isinstance(gold.get("finish_now"), bool):
            plan["gold"]["finish_now"] = gold["finish_now"]
        for key, lo, hi, cast in (("min_minutes_left", 0, 24 * 60, int), ("reserve", 0, 100000, int),
                                  ("capital_reserve", 0, 100000, int), ("max_per_tick", 0, 10, int)):
            v = _num(gold, key, lo, hi, cast)
            if v is not None:
                plan["gold"][key] = v

    share = raw.get("share")
    if isinstance(share, dict):
        for key in ("enabled", "allies"):
            if isinstance(share.get(key), bool):
                plan["share"][key] = share[key]
        for key, lo, hi, cast in (("young_ratio", 0, 1, float), ("donor_keep_share", 0, 0.95, float),
                                  ("fill_to", 0.1, 1, float), ("min_shipment", 0, 100000, int),
                                  ("ally_keep_share", 0, 0.95, float)):
            v = _num(share, key, lo, hi, cast)
            if v is not None:
                plan["share"][key] = v
    return plan


def load(path, max_age_hours):
    """Return (plan, source) where source is 'file (...)' or 'default (<reason>)'."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return validate({}), "default (no plan file)"
    except (OSError, ValueError) as e:
        log.warning("plan file unreadable, using default: %s", e)
        return validate({}), "default (plan file unreadable)"

    created = raw.get("created") if isinstance(raw, dict) else None
    if not isinstance(created, (int, float)):
        return validate({}), "default (plan file has no 'created' time)"
    age_h = (time.time() - created) / 3600
    # a hand-written plan ("source": "manual...") stays in force until replaced, so the bot
    # keeps following it while the advisor PC is offline
    manual = str(raw.get("source", "")).startswith("manual")
    if age_h > max_age_hours and not manual:
        return validate({}), f"default (plan file is {age_h:.1f}h old)"
    return validate(raw), f"file ({raw.get('source', 'unknown')}, {age_h:.1f}h old)"
