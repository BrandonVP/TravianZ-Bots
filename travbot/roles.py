"""Village roles (playbook: specialise villages) and the buildings each role needs in each phase.

capital  the account's capital: the cropper that feeds the army, Town Hall (culture points), Armoury, and a
         Treasury: it hides village gold from raiders (Brandon 2026-11-13), and the capital keeps the most gold
army     one village (two from 8 villages on) that holds the attack army: Barracks, Stable, Workshop,
         Academy, Blacksmith, Rally Point, Champion's Hall
Every village gets a small Treasury from the relics phase, for its gold. The bots take no relics themselves:
relics and the Wonder are the human members' to take; the bots clear the way and defend (Brandon 2026-11-13).
feeder   the rest: economy, Marketplace (feeding the army village and allies), small celebrations

The role targets go in front of the plan's own building targets for that village (the advisor's plan applies to
every village alike). Requirements the game enforces are respected by the order of the lists; anything the game
does not offer yet is simply skipped that round.
"""
import json

from . import rules

PHASE_ORDER = ["opening", "second village", "growth", "relics", "wonder"]

TARGETS = {
    # role -> phase from which a list applies -> [(building, level)]
    "capital": {
        "growth": [("Main Building", 10), ("Treasury", 5), ("Barracks", 3), ("Academy", 10), ("Town Hall", 10),
                   ("Grain Mill", 5), ("Bakery", 5), ("Armoury", 5), ("Treasury", 10)],
        "relics": [("Hero's Mansion", 1), ("Armoury", 10), ("Palisade", 15)],   # a Hall: the steward lives here
        "wonder": [("Armoury", 15), ("Palisade", 20)],
    },
    "army": {
        "second village": [("Rally Point", 3), ("Barracks", 3)],
        "growth": [("Main Building", 10), ("Academy", 5), ("Blacksmith", 3), ("Stable", 5), ("Academy", 10),
                   ("Hero's Mansion", 1), ("Workshop", 1), ("Blacksmith", 10), ("Barracks", 10), ("Stable", 10),
                   ("Rally Point", 10), ("Hero's Mansion", 5)],
        "relics": [("Treasury", 5), ("Hero's Mansion", 10), ("Workshop", 10), ("Blacksmith", 15),
                   ("Barracks", 15), ("Stable", 15)],
        "wonder": [("Hero's Mansion", 20), ("Blacksmith", 20), ("Barracks", 20), ("Stable", 20), ("Rally Point", 20)],
    },
    "feeder": {
        "growth": [("Marketplace", 10), ("Town Hall", 1)],
        "relics": [("Treasury", 5), ("Marketplace", 15), ("Trade Office", 5)],
        "wonder": [("Marketplace", 20), ("Trade Office", 10)],
    },
}


def phase_index(phase):
    return PHASE_ORDER.index(phase) if phase in PHASE_ORDER else 2


def targets(role, phase):
    """[{name, level}] for a role up to and including `phase` (earlier phases first)."""
    out = []
    for ph in PHASE_ORDER[:phase_index(phase) + 1]:
        out += [{"name": n, "level": lv} for n, lv in TARGETS.get(role, {}).get(ph, [])]
    return out


def overlay(plan, role, phase, guardian=False):
    """The plan for one village: its role's targets first, then the plan's own. The guardian champion's village
    gets a Champion's Hall (level 1) so the champion can live there."""
    if not plan.get("roles", {}).get("enabled", True):
        return plan
    extra = [{"name": "Hero's Mansion", "level": 1}] if guardian else []
    return {**plan, "building_targets": extra + targets(role, phase) + plan["building_targets"]}


class Roles:
    def __init__(self, state_dir):
        self.path = state_dir / "roles.json"
        try:
            self.data = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {"army": []}

    def set_guardian(self, vid):
        if self.data.get("guardian") != vid:
            self.data["guardian"] = vid
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)

    def assign(self, villages, capital):
        """{village id: role}. `villages`: [{"id", "state"}]. The army village choice is kept once made (troops
        and buildings collect there); a new one is picked only if it is gone."""
        ids = [v["id"] for v in villages]
        if len(villages) == 1:
            return {ids[0]: "army"}   # one village does everything; it trains the army too
        want = 1 if len(villages) < 8 else 2
        army = [vid for vid in self.data.get("army", []) if vid in ids and vid != capital][:want]
        others = sorted((v for v in villages if v["id"] != capital and v["id"] not in army),
                        key=lambda v: -rules.total_production(v["state"]))
        while len(army) < want and others:
            army.append(others.pop(0)["id"])
        if army != self.data.get("army"):
            self.data["army"] = army
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data), encoding="utf-8")
            tmp.replace(self.path)
        return {vid: ("capital" if vid == capital else "army" if vid in army else "feeder") for vid in ids}
