"""Unit ids -> names for the three classic tribes (ids as in the game: (tribe-1)*10 + 1..10).

Empires of Rome names (renamed 2026-10-24); keep in step with U1..U30 in the game's
GameEngine/Lang/en.php. Used only for logs, the status page and the advisor prompt -
the bot trains by unit id, never by name.
"""

_NAMES = {
    1: ["Hastatus", "Triarius", "Princeps", "Speculator", "Eques",
        "Cataphract", "Aries", "Onager", "Legatus", "Colonus"],
    2: ["Clubman", "Spearman", "Axeman", "Scout", "Paladin",
        "Teutonic Knight", "Ram", "Catapult", "Chief", "Settler"],
    3: ["Phalanx", "Swordsman", "Pathfinder", "Gallic Horseman", "Mounted Druid",
        "Noble Cavalry", "Ram", "Trebuchet", "Chieftain", "Settler"],
}

_PLURALS = {
    "Hastatus": "Hastati", "Triarius": "Triarii", "Princeps": "Principes", "Speculator": "Speculatores",
    "Eques": "Equites", "Legatus": "Legati", "Colonus": "Coloni",
    "Clubman": "Clubmen", "Spearman": "Spearmen", "Axeman": "Axemen",
    "Phalanx": "Phalanxes", "Swordsman": "Swordsmen", "Gallic Horseman": "Gallic Horsemen",
    "Mounted Druid": "Mounted Druids", "Noble Cavalry": "Noble Cavalry",
    "Funditor": "Funditores", "Bowman": "Bowmen", "Hunter": "Hunters",
}


# Archery Range units (game: GameEngine/UnitRegistry.php, U201..U282 in en.php):
# id 200 + (tribe-1)*10 + k; k=1 the archer (no research), k=2 the skirmisher
# (Academy research, high defence against ranged - the bots' archer counter)
_EXTRA = {
    201: "Sagittarius", 202: "Funditor", 211: "Bowman", 212: "Javelin Thrower",
    221: "Archer", 222: "Slinger", 251: "Foot Archer", 252: "Steppe Javelineer",
    261: "Nubian Archer", 262: "Chariot Archer", 271: "Cretan Archer", 272: "Peltast",
    281: "Hunter", 282: "Axe Thrower",
}


def skirmisher(tribe):
    """The tribe's second Archery Range unit (e.g. Gauls -> 222 Slinger)."""
    return 200 + (tribe - 1) * 10 + 2


def extra_slot(unit_id):
    """Rally Point form field number of an Archery Range unit (t12, t13 ...)."""
    return 11 + (unit_id - 200) % 10


def name(unit_id):
    if unit_id in _EXTRA:
        return _EXTRA[unit_id]
    tribe, idx = divmod(unit_id - 1, 10)
    names = _NAMES.get(tribe + 1)
    return names[idx] if names else f"unit {unit_id}"


def plural(unit_name):
    return _PLURALS.get(unit_name, unit_name + "s")
