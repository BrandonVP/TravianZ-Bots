"""Pure decision logic: given village state + plan, list what to try building next.

Candidates are tried in order; the game itself is the final judge (it only offers an
upgrade link when the queue has room and resources suffice).
"""

WALLS = ("City Wall", "Earth Wall", "Palisade")
RALLY_SLOT, WALL_SLOT = 39, 40
INNER_SLOTS = list(range(19, 39)) + list(range(41, 45))   # 41-44: extra building slots
# servers with archers: the skirmisher needs Academy 5 + Archery Range 5 (Barracks 3 for the Range)
ARCHERY_TARGETS = (("Barracks", 3), ("Academy", 5), ("Archery Range", 5))
CANDIDATES_PER_CATEGORY = 3
# Building targets can be blocked by requirements (the game offers no link), so look
# further down the list before giving up on this tick.
BUILDING_CANDIDATES = 6


def _hours_to_full(res):
    if res["prod"] <= 0:
        return float("inf")
    return max(res["cap"] - res["amount"], 0) / res["prod"]


def effective_buildings(state):
    """slot -> {"name", "level"} including construction that is queued but not finished."""
    out = {slot: dict(b) for slot, b in state["buildings"].items() if b}
    for job in state["queue"]:
        slot, lvl = job["slot"], job["to_level"] or 0
        if 19 <= slot <= 44:
            current = out.get(slot)
            if current is None:
                out[slot] = {"name": job["name"], "level": lvl}
            else:
                current["level"] = max(current["level"], lvl)
    return out


def field_candidates(state, plan):
    res = state["resources"]
    queued = {job["slot"] for job in state["queue"]}
    fields = [f for f in state["fields"]
              if f["slot"] not in queued and f["level"] < plan["field_max_level"]]

    if res["crop"]["prod"] < plan["min_net_crop"]:
        crop = sorted((f for f in fields if f["kind"] == "crop"), key=lambda f: f["level"])
        return [{"action": "upgrade", "slot": f["slot"], "name": "Cropland",
                 "reason": f"net crop {res['crop']['prod']}/h below {plan['min_net_crop']}"}
                for f in crop[:CANDIDATES_PER_CATEGORY]]

    # gold mines (village gold for Plus and finishing builds): keep them level
    # with the weakest resource field, cheapest mine first
    mines = sorted((f for f in fields if f["kind"] == "gold"), key=lambda f: f["level"])
    fields = [f for f in fields if f["kind"] != "gold"]
    if mines and (not fields or mines[0]["level"] <= min(f["level"] for f in fields)):
        return [{"action": "upgrade", "slot": f["slot"], "name": "Gold Mine",
                 "reason": f"gold mine level {f['level']} behind the resource fields"}
                for f in mines[:CANDIDATES_PER_CATEGORY]]

    weights = plan["resource_weights"]

    def score(f):
        # lowest weighted production first, then lowest level
        return (res[f["kind"]]["prod"] / weights[f["kind"]], f["level"])

    fields = sorted((f for f in fields if weights[f["kind"]] > 0), key=score)
    return [{"action": "upgrade", "slot": f["slot"], "name": f["kind"] + " field",
             "reason": f"{f['kind']} production {res[f['kind']]['prod']}/h, field level {f['level']}"}
            for f in fields[:CANDIDATES_PER_CATEGORY]]


def _free_slot(name, buildings):
    if name == "Rally Point":
        return RALLY_SLOT if RALLY_SLOT not in buildings else None
    if name in WALLS:
        return WALL_SLOT if WALL_SLOT not in buildings else None
    return next((s for s in INNER_SLOTS if s not in buildings), None)


def building_candidates(state, plan):
    buildings = effective_buildings(state)
    by_name = {}
    for slot, b in buildings.items():
        if b["name"] not in by_name or b["level"] > by_name[b["name"]][1]:
            by_name[b["name"]] = (slot, b["level"])

    wanted = []  # (name, reason)
    res = state["resources"]
    buffer_h = plan["storage_buffer_hours"]
    whs = min(_hours_to_full(res[k]) for k in ("wood", "clay", "iron"))
    if whs < buffer_h:
        wanted.append(("Warehouse", f"storage full in {whs:.1f}h"))
    gran = _hours_to_full(res["crop"])
    if gran < buffer_h:
        wanted.append(("Granary", f"granary full in {gran:.1f}h"))
    for t in plan["building_targets"]:
        if by_name.get(t["name"], (None, 0))[1] < t["level"]:
            wanted.append((t["name"], f"target level {t['level']}"))
    if plan.get("archers") and plan["defense"]["ranged_target"] > 0:
        # after the plan's own targets: what the anti-archer skirmishers need
        for name, level in ARCHERY_TARGETS:
            if by_name.get(name, (None, 0))[1] < level:
                wanted.append((name, f"archer defence needs {name} {level}"))

    out, seen = [], set()
    for name, reason in wanted:
        if name in seen:
            continue
        seen.add(name)
        if name in by_name:
            out.append({"action": "upgrade", "slot": by_name[name][0], "name": name, "reason": reason})
        else:
            slot = _free_slot(name, buildings)
            if slot is None:
                continue
            out.append({"action": "construct", "slot": slot, "name": name, "reason": reason})
            buildings[slot] = {"name": name, "level": 0}  # reserve the site for this tick
        if len(out) >= BUILDING_CANDIDATES:
            break
    return out


def total_production(state):
    return sum(max(state["resources"][k]["prod"], 0) for k in ("wood", "clay", "iron", "crop"))


def finish_choices(queue, gold, reserve, gold_plan):
    """Running jobs worth finishing with gold: longest first, while gold stays above `reserve`.

    When the village gold is nearly full (it would be wasted), shorter jobs qualify too.
    """
    if not gold_plan["finish_now"] or not gold:
        return []
    min_left = gold_plan["min_minutes_left"] * 60
    if gold["cap"] and gold["amount"] >= 0.9 * gold["cap"]:
        min_left = min(min_left, 5 * 60)
    jobs = sorted((j for j in queue if j.get("finish_href") and j.get("seconds_left", 0) >= min_left),
                  key=lambda j: -j["seconds_left"])
    out, left = [], gold["amount"]
    for j in jobs[:gold_plan["max_per_tick"]]:
        price = j.get("finish_price") or 50
        if left - price < reserve:
            break
        out.append(j)
        left -= price
    return out


def is_young(state, richest_production, share_plan):
    """A village far behind the richest one: it gets resources shipped in."""
    return total_production(state) < share_plan["young_ratio"] * richest_production


def shipment(donor, recipient, merchants, carry, share_plan, reserved_cost=None):
    """Resources to ship from donor to recipient: fills the recipient's storage up to `fill_to`,
    never takes the donor below `donor_keep_share` of its capacity (or below what it saves for),
    and fits the free merchants. Returns {"wood","clay","iron","crop"} or None if not worth it."""
    room, spare = {}, {}
    for k in ("wood", "clay", "iron", "crop"):
        r = recipient["resources"][k]
        room[k] = max(0, int(share_plan["fill_to"] * r["cap"]) - r["amount"])
        d = donor["resources"][k]
        keep = max(int(share_plan["donor_keep_share"] * d["cap"]), (reserved_cost or {}).get(k, 0))
        spare[k] = max(0, d["amount"] - keep)
    want = {k: min(room[k], spare[k]) for k in room}
    total = sum(want.values())
    space = merchants * carry
    if total <= 0 or space <= 0:
        return None
    if total > space:  # scale down evenly to what the merchants can carry
        f = space / total
        want = {k: int(v * f) for k, v in want.items()}
    if sum(want.values()) < share_plan["min_shipment"]:
        return None
    return want


def basic_defender(tribe):
    """First unit of the tribe (e.g. Gauls -> 21 Phalanx); trainable without research."""
    return (tribe - 1) * 10 + 1


def settler_unit(tribe):
    return tribe * 10


def find_slot(state, name):
    """Slot of the (highest-level) building with this name, or None."""
    best = None
    for slot, b in effective_buildings(state).items():
        if b["name"] == name and b["level"] > 0 and (best is None or b["level"] > best[1]):
            best = (slot, b["level"])
    return best[0] if best else None


def building_level(state, name):
    lvl = 0
    for b in state["buildings"].values():
        if b and b["name"] == name:
            lvl = max(lvl, b["level"])
    return lvl


def troop_order(unit_cost, have, in_training, target, resources, share):
    """How many defenders to order now: up to the target, within `share` of each resource."""
    missing = target - have - in_training
    if missing <= 0 or not unit_cost:
        return 0
    afford = min((int(resources[k]["amount"] * share) // c) if c else missing
                 for k, c in unit_cost.items())
    return max(0, min(missing, afford))


def subtract(resources, cost):
    """Copy of resources with `cost` spent."""
    out = {k: dict(v) for k, v in resources.items()}
    for k, c in (cost or {}).items():
        out[k]["amount"] -= c
    return out


SAVE_UP_SLACK_S = 600


def delays_reserved(reserved_cost, resources, spend_cost, slack_s=SAVE_UP_SLACK_S):
    """True if spending `spend_cost` now would push the reserved item back by more than slack_s."""
    before = seconds_until_affordable(reserved_cost, resources)
    after = seconds_until_affordable(reserved_cost, subtract(resources, spend_cost))
    return after - before > slack_s


def seconds_until_affordable(cost, resources):
    """How long until `cost` is affordable at current production (inf if never)."""
    worst = 0.0
    for k, need in cost.items():
        short = need - resources[k]["amount"]
        if short <= 0:
            continue
        if resources[k]["prod"] <= 0:
            return float("inf")
        worst = max(worst, short / resources[k]["prod"] * 3600)
    return worst
