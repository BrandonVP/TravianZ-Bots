import json, pathlib, sys
from travbot.client import GameClient
from travbot import rules, parse
d = pathlib.Path(sys.argv[1]); acc = json.loads((d / "bot-account.json").read_text())
roles = json.loads((d / "roles.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"]); c.login()
for vid in roles["army"]:
    c.select_village(vid); st = c.village(); ac = rules.find_slot(st, "Academy")
    prog = parse._soup(c.get(f"build.php?id={ac}")).find("table", class_="under_progress") if ac else None
    print(vid, "academy", ac, "in progress:", prog.get_text(" ", strip=True)[:150] if prog else None)
