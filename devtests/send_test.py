"""REAL sends on the DEV game: 1 basic defender between own villages, and an attack the game must refuse."""
import json, pathlib, sys
from travbot.client import GameClient
from travbot import rules, parse
d = pathlib.Path(sys.argv[1]); acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"]); c.login()
st = c.village()
ac = rules.find_slot(st, "Academy")
html = c.get(f"build.php?id={ac}")
prog = parse._soup(html).find("table", class_="under_progress")
print("academy in progress:", prog.get_text(" ", strip=True)[:120] if prog else None)
vs = [v for v in st["villages"] if v.get("x") is not None]
src, dst = vs[0], vs[1]
c.select_village(src["id"]); before = c.village()["troops"]
r = c.send_army(dst["x"], dst["y"], 2, {1: 1})
after = c.village()["troops"]
print("reinforce 1:", r, "| basic defenders before/after:", before.get(11), after.get(11))
plan = json.loads((d / "plan.json").read_text())
print("new plan sections:", {k: plan.get(k) for k in ("army", "culture", "wonder")})
r = c.send_army(0, 0, 3, {1: 1})     # (0|0) is usually an admin/Taskmaster or empty tile
print("attack (0|0):", r)
