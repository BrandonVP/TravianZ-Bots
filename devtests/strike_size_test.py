"""The counter-strike is never a token force: at least counter.min_share of the attack at home (2026-11-15)."""
from travbot import combat, plan as planmod

cfg = planmod.validate({})["counter"]
assert cfg["min_share"] == 0.5, cfg
off = {u: n for u, n in zip(combat.offence_units(3)[:2], (40, 20))}     # Gauls: two kinds of attack troops
inf, cav = combat.attack_power(off)
total = inf + cav

def force(need, theirs):
    floor = cfg["min_share"] * total
    return combat.size_force(off, max(need, floor, min(cfg["size_factor"] * theirs, total)))

old = combat.size_force(off, 0)
assert all(n == 1 for n in old.values()), old            # what was sent before: one of each
f = force(0, 0)                                          # empty village, attacker's force unknown
assert sum(combat.attack_power(f)) >= 0.5 * total, f
assert all(f[u] >= off[u] // 2 for u in off), f
f = force(0.9 * total, 0)                                # a strong defence still decides the size
assert sum(combat.attack_power(f)) >= 0.9 * total, f
assert force(2 * total, 0) is None                       # too strong: no strike
print("strike_size_test: ok", force(0, 0))
