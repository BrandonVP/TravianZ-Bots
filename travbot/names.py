"""Empires of Rome building names <-> the bot's internal (classic) names.

The game renamed its buildings on 2026-10-24 (Main Building -> Town Center, ...).
The bot keeps its internal names (rules, default plan, saved plan files) and
translates at the edges: canon() for names read from game pages and plans,
display() / display_text() for logs, the status page and the advisor prompt.
Old and new page names both work, so the bot runs on either game version.
"""
import re

NEW_TO_OLD = {
    "Lumber Camp": "Woodcutter", "Wheat Field": "Cropland", "Forge": "Iron Foundry",
    "Storehouse": "Warehouse", "Grand Storehouse": "Great Warehouse", "Grand Granary": "Great Granary",
    "Muster Field": "Rally Point", "Siege Workshop": "Workshop", "Legion Camp": "Great Barracks",
    "Cavalry Grounds": "Great Stable", "Grand Siege Works": "Great Workshop",
    "Training Arena": "Tournament Square", "Snare Pits": "Trapper", "Infirmary": "Hospital",
    "Great Infirmary": "Big Hospital", "Horse Trough": "Horse Drinking Trough", "Town Center": "Main Building",
    "Merchants' Guild": "Trade Office", "Villa": "Residence", "Warlord's Camp": "Command Center",
    "Hidden Cellar": "Cranny", "Masons' Guild": "Stonemason's Lodge", "Mead Hall": "Brewery",
    "Aqueduct": "Waterworks", "Earthwork": "Earth Wall", "Shield Wall": "Barricade",
    "Champion's Hall": "Hero's Mansion", "Wonder of the Ancients": "Wonder of the World",
}
OLD_TO_NEW = {old: new for new, old in NEW_TO_OLD.items()}

# longest first, so "Great Warehouse" is handled before "Warehouse"
_TEXT = re.compile(r"\b(" + "|".join(re.escape(o) for o in sorted(OLD_TO_NEW, key=len, reverse=True)) + r")\b")


def canon(name):
    """Name as shown by the game (old or new) -> the bot's internal name."""
    name = (name or "").strip().replace("`", "'")
    return NEW_TO_OLD.get(name, name)


def display(name):
    """Internal name -> the name the game shows today."""
    return OLD_TO_NEW.get(name, name)


def display_text(text):
    """Swap every internal building name in a piece of text for today's name."""
    return _TEXT.sub(lambda m: OLD_TO_NEW[m.group(1)], text)
