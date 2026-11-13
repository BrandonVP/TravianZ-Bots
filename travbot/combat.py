"""Rough battle estimates from the game's unit stats (travbot/unitstats.py), for deciding whether a strike is
worth it. Deliberately conservative: the game's real formula (wall fortification, upgrades, champion, morale)
is not copied; callers ask for a clear margin instead.
"""
from .unitstats import STATS, CAVALRY

SCOUTS = {1: 4, 2: 14, 3: 23}   # unit ids
SIEGE = (7, 8)          # tribe slots: ram, catapult
WALL_FACTOR = 1.025     # defence bonus per wall level (classic values are 1.020-1.030)
BASE_DEFENCE = 10       # an empty village still defends a little


def unit_id(tribe, slot):
    return (tribe - 1) * 10 + slot


def slot_of(uid):
    return (uid - 1) % 10 + 1


def offence_units(tribe):
    """The tribe's attacking units (not scouts, siege, chiefs or settlers, and not the first unit, which the bots
    keep as their basic defender - for Teutons the Clubman), best attack per resource first."""
    out = []
    for slot in range(2, 7):
        u = unit_id(tribe, slot)
        s = STATS.get(u)
        if not s or s["atk"] <= 0 or u == SCOUTS.get(tribe):
            continue
        if s["atk"] < max(s["di"], s["dc"]):
            continue  # a defensive unit
        cost = s["wood"] + s["clay"] + s["iron"] + s["crop"]
        out.append((s["atk"] / cost, u))
    return [u for _, u in sorted(out, reverse=True)]


def attack_power(troops):
    """(infantry attack, cavalry attack) of {uid: n}."""
    inf = cav = 0
    for u, n in troops.items():
        s = STATS.get(int(u))
        if not s or n <= 0:
            continue
        if int(u) in CAVALRY:
            cav += s["atk"] * n
        else:
            inf += s["atk"] * n
    return inf, cav


def defence_power(troops, inf_share=0.5, wall=0):
    """Defence of {uid: n} against an attack that is `inf_share` infantry."""
    d = 0.0
    for u, n in troops.items():
        s = STATS.get(int(u))
        if s and n > 0:
            d += n * (s["di"] * inf_share + s["dc"] * (1 - inf_share))
    return d * (WALL_FACTOR ** max(0, wall or 0)) + BASE_DEFENCE


def battle(att, dfn, wall=0):
    """Estimate of attackers `att` against defenders `dfn` ({uid: n} each)."""
    inf, cav = attack_power(att)
    a = inf + cav
    if a <= 0:
        return {"attack": 0, "defence": defence_power(dfn, 0.5, wall), "win": False, "att_loss": 1.0, "def_loss": 0.0}
    d = defence_power(dfn, inf / a, wall)
    win = a > d
    return {"attack": a, "defence": d, "win": win,
            "att_loss": min(1.0, (d / a) ** 1.5) if win else 1.0,
            "def_loss": 1.0 if win else min(1.0, (a / d) ** 1.5)}


def size_force(available, needed_attack):
    """The smallest share of `available` ({uid: n}, attack units) whose attack reaches `needed_attack`, keeping the
    mix; returns {uid: n} or None when everything together is not enough."""
    inf, cav = attack_power(available)
    total = inf + cav
    if total <= 0 or total < needed_attack:
        return None
    share = min(1.0, needed_attack / total)
    out = {u: max(1, int(round(n * share + 0.4999))) for u, n in available.items() if n > 0}
    return {u: min(n, available[u]) for u, n in out.items()}


def upkeep(troops):
    return sum(STATS.get(int(u), {}).get("pop", 1) * n for u, n in troops.items())
