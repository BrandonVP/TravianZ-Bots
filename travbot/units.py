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
}


def name(unit_id):
    tribe, idx = divmod(unit_id - 1, 10)
    names = _NAMES.get(tribe + 1)
    return names[idx] if names else f"unit {unit_id}"


def plural(unit_name):
    return _PLURALS.get(unit_name, unit_name + "s")
