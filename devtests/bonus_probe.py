"""Production bonus / NPC probe against the dev game: python devtests/bonus_probe.py state  (reads only)."""
import json, pathlib, sys
from travbot.client import GameClient
from travbot import rules, parse
d = pathlib.Path(sys.argv[1]); acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"]); c.login()
cap = c.capital_id(); print("capital id:", cap)
if cap: c.select_village(cap)
st = c.village(); print("gold:", st["gold"], "| villages:", len(st["villages"]))
html = c.get("build.php?gid=15")
print("bonuses:", parse.parse_bonuses(html))
if parse.parse_bonuses(html) is None:
    i = html.find("tz-mbplus"); print("tz-mbplus at", i, html[i - 200:i + 600] if i >= 0 else html[:300])
slot = rules.find_slot(st, "Marketplace"); print("marketplace slot:", slot)
if slot:
    print("npc form:", c.npc_form(slot))
