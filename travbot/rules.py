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
FIELD_LEVEL_CAP = 25          # resource fields go to 25 in every village on this server
SPARE_FILL = 0.5              # a village with nothing left to build and storage this full keeps growing fields
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

    # the plan's field_max_level is one number for every village; a strong village that has reached it
    # and has nothing left to build would just fill up (the capitals sat at 80% with an empty queue).
    # Then spare resources go into fields up to the game's cap.
    spare = ""
    if not fields and _has_spare(state, plan):
        fields = [f for f in state["fields"] if f["slot"] not in queued and f["kind"] != "gold"
                  and f["level"] < FIELD_LEVEL_CAP]
        spare = ", spare resources"

    weights = plan["resource_weights"]

    # Upgrade the resource the village has least of and leave the one it has most of alone (Brandon 2026-11-24,
    # how he plays the early game). Until then the bot picked the lowest weighted production, which always meant
    # crop (production is net of upkeep): every bot sat on ~2,500 crop, saving hours for the next crop field while
    # wood and clay ran out. Crop still comes first when net crop runs low (the min_net_crop branch above).
    def have(kind):  # stock plus an hour of production, per weight
        return (res[kind]["amount"] + max(res[kind]["prod"], 0)) / weights[kind]

    kinds = {f["kind"] for f in fields if weights[f["kind"]] > 0}
    if len(kinds) > 1:
        kinds.discard(max(kinds, key=have))
    fields = sorted((f for f in fields if f["kind"] in kinds), key=lambda f: (have(f["kind"]), f["level"]))
    return [{"action": "upgrade", "slot": f["slot"], "name": f["kind"] + " field",
             "reason": f"least {f['kind']} in stock ({int(res[f['kind']]['amount'])}), field level {f['level']}{spare}"}
            for f in fields[:CANDIDATES_PER_CATEGORY]]


def _has_spare(state, plan):
    """Storage on average at least SPARE_FILL full and no building left to work on."""
    res = state["resources"]
    fill = sum(res[k]["amount"] / max(res[k]["cap"], 1) for k in ("wood", "clay", "iron", "crop")) / 4
    return fill >= SPARE_FILL and not building_candidates(state, plan)


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
    level = lambda n: by_name.get(n, (None, 0))[1]
    gold = state.get("gold") or {}
    if plan["gold"].get("npc", True) and not level("Marketplace") and gold.get("amount", 0) >= NPC_PRICE \
            and level("Main Building") >= 3 and level("Warehouse") >= 1 and level("Granary") >= 1:
        wanted.append(("Marketplace", "NPC trades need a Marketplace"))
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
    """Running jobs worth finishing with gold, best value first, while gold stays above `reserve`.

    Finish now is priced by the time left (10 gold for minutes ... 700 for a week), so the bot only
    buys time where it is cheap: a job must save at least `min_minutes_per_gold` minutes per gold and
    cost at most `max_finish_price`. With the game's curve that is roughly 4.5 to 11 hours left.
    When the village's gold is nearly full (it would be wasted at the cap) the price limit doubles and
    the value bar halves.
    """
    if not gold_plan["finish_now"] or not gold:
        return []
    max_price = gold_plan["max_finish_price"]
    min_value = gold_plan["min_minutes_per_gold"]
    if gold["cap"] and gold["amount"] >= 0.9 * gold["cap"]:
        max_price, min_value = max_price * 2, min_value / 2
    jobs = []
    for j in queue:
        price = j.get("finish_price")
        if not j.get("finish_href") or not price or price > max_price:
            continue
        value = j.get("seconds_left", 0) / 60 / price   # minutes saved per gold
        if value >= min_value:
            jobs.append((value, j))
    jobs.sort(key=lambda vj: -vj[0])
    out, left = [], gold["amount"]
    for _, j in jobs:
        if len(out) >= gold_plan["max_per_tick"]:
            break
        if left - j["finish_price"] < reserve:
            continue   # too dear for what is left: a cheaper one may still fit
        out.append(j)
        left -= j["finish_price"]
    return out


# --- production bonuses and NPC trades (2026-11-24) ---------------------------------------------------------
# The capital's gold buys +25% production of one resource for 7 days (Town Center, 1,000 gold). That is the best
# long-run use of gold at this stage (Brandon's own strategy); an NPC trade (250 gold, needs a Marketplace) is for
# when the stock is lopsided and the next build waits on one resource. Finish now stays for cheap, long jobs.
BONUS_PRICE = 1000
BONUS_DAYS = 7
NPC_PRICE = 250


def bonus_keep(bonuses, gold_prod, price=BONUS_PRICE):
    """Gold to keep now so every running bonus can be renewed when it ends, counting the gold mined until then.
    `bonuses`: {kind: seconds left} (0 = not running)."""
    keep, n = 0.0, 0
    for left in sorted(s for s in bonuses.values() if s > 0):
        n += 1
        keep = max(keep, n * price - max(gold_prod, 0) * left / 3600)
    return int(max(keep, 0))


def bonus_choice(state, plan, bonuses, gold):
    """(kind, reason) of the production bonus to buy or extend now, or None.

    The bonus goes to the resource the village is shortest of (the same ranking as the fields; crop only while net
    crop is low). A bonus in its last bonus_renew_hours competes again: it is extended if its resource is still the
    shortest, else it runs out and the gold goes to the shorter one. Bought only if the gold left still covers the
    renewals of the bonuses that keep running."""
    g = plan["gold"]
    if not g.get("production_bonus", True) or gold is None:
        return None
    have, prod = gold["amount"], gold.get("prod", 0)
    if have < BONUS_PRICE:
        return None
    renew_s = g.get("bonus_renew_hours", 2) * 3600
    ending = {k for k, s in bonuses.items() if 0 < s <= renew_s}
    free = lambda k: not bonuses.get(k) or k in ending
    res, weights = state["resources"], plan["resource_weights"]
    want = sorted((k for k in ("wood", "clay", "iron") if free(k) and weights.get(k, 1) > 0),
                  key=lambda k: (res[k]["amount"] + max(res[k]["prod"], 0)) / weights.get(k, 1))
    if res["crop"]["prod"] < plan["min_net_crop"] and free("crop"):
        want.insert(0, "crop")
    if not want:
        return None
    kind = want[0]
    after = {k: s for k, s in bonuses.items() if s > 0 and k not in ending}
    after[kind] = bonuses.get(kind, 0) + BONUS_DAYS * 86400
    if have - BONUS_PRICE < bonus_keep(after, prod):
        return None
    if kind in ending:
        return kind, f"+25% {kind} ends in {int(bonuses[kind] // 60)} min and {kind} is still the shortest"
    return kind, f"least {kind} in stock ({int(res[kind]['amount'])}, +{res[kind]['prod']}/h)"


def npc_split(cost, resources, caps):
    """New stock for an NPC trade that pays `cost` now: each resource gets what the cost needs, the rest is
    shared out evenly (within storage). None if the village does not hold enough in total."""
    kinds = ("wood", "clay", "iron", "crop")
    have = {k: int(resources[k]["amount"]) for k in kinds}
    total = sum(have.values())
    need = {k: int(cost.get(k, 0)) for k in kinds}
    if sum(need.values()) > total or any(need[k] > caps[k] for k in kinds):
        return None
    out, left = dict(need), total - sum(need.values())
    while left > 0:
        open_ = [k for k in kinds if out[k] < caps[k]]
        if not open_:
            break
        share = max(left // len(open_), 1)
        for k in open_:
            add = min(share, caps[k] - out[k], left)
            out[k] += add
            left -= add
            if left <= 0:
                break
    return out if sum(out.values()) == total else None


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
    afford = min(((int(resources[k]["amount"] * share) // c) if c else missing)
                 for k, c in unit_cost.items() if k in resources)   # a cost the stock doesn't track is ignored
    return max(0, min(missing, afford))


def subtract(resources, cost):
    """Copy of resources with `cost` spent."""
    out = {k: dict(v) for k, v in resources.items()}
    for k, c in (cost or {}).items():
        if k in out:
            out[k]["amount"] -= c
    return out


SAVE_UP_SLACK_S = 600


def delays_reserved(reserved_cost, resources, spend_cost, slack_s=SAVE_UP_SLACK_S):
    """True if spending `spend_cost` now would push the reserved item back by more than slack_s."""
    before = seconds_until_affordable(reserved_cost, resources)
    after = seconds_until_affordable(reserved_cost, subtract(resources, spend_cost))
    return after - before > slack_s


def stock(state):
    """The village's resources plus its gold ({"amount", "prod"}), for costs that include gold."""
    res = dict(state["resources"])
    g = state.get("gold")
    if g:
        res["gold"] = {"amount": g["amount"], "cap": g.get("cap"), "prod": g.get("prod", 0)}
    return res


def seconds_until_affordable(cost, resources):
    """How long until `cost` is affordable at current production (inf if never).
    A cost the stock doesn't track (gold with village gold off) is ignored."""
    worst = 0.0
    for k, need in cost.items():
        if k not in resources:
            continue
        short = need - resources[k]["amount"]
        if short <= 0:
            continue
        if resources[k]["prod"] <= 0:
            return float("inf")
        worst = max(worst, short / resources[k]["prod"] * 3600)
    return worst
