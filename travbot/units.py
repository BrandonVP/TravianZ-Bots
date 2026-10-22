"""Unit ids -> names for the three classic tribes (ids as in the game: (tribe-1)*10 + 1..10)."""

_NAMES = {
    1: ["Legionnaire", "Praetorian", "Imperian", "Equites Legati", "Equites Imperatoris",
        "Equites Caesaris", "Battering Ram", "Fire Catapult", "Senator", "Settler"],
    2: ["Clubswinger", "Spearman", "Axeman", "Scout", "Paladin",
        "Teutonic Knight", "Ram", "Catapult", "Chief", "Settler"],
    3: ["Phalanx", "Swordsman", "Pathfinder", "Theutates Thunder", "Druidrider",
        "Haeduan", "Ram", "Trebuchet", "Chieftain", "Settler"],
}


def name(unit_id):
    tribe, idx = divmod(unit_id - 1, 10)
    names = _NAMES.get(tribe + 1)
    return names[idx] if names else f"unit {unit_id}"
