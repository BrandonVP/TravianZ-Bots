"""Read-only checks of the new game paths on the DEV game: python -m devtests.probe [state dir]"""
import json, pathlib, sys
from travbot.client import GameClient
from travbot import parse, rules, combat, world
d = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "state")
acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"])
c.login()
print("capital wref:", c.capital_id())
print("world day:", world.WorldClock().day(c))
feed = c.feed("ally")
print("feed alliance:", (feed.get("alliance") or {}).get("name"), "members:", [(m["name"], m["bot"], len(m["villages"])) for m in feed.get("members", [])])
v0 = feed["members"][0]["villages"][0] if feed.get("members") else {}
print("feed village keys:", sorted(v0))
print("incoming:", feed.get("incoming"))
inbox = c.feed("inbox")
print("inbox:", [(m["from_name"], m["subject"]) for m in (inbox or {}).get("messages", [])][:5])
st = c.village()
for b in ("Blacksmith", "Armoury", "Academy", "Town Hall", "Stable", "Workshop", "Rally Point", "Barracks"):
    print(f"  {b}: slot {rules.find_slot(st, b)} level {rules.building_level(st, b)}")
for b in ("Blacksmith", "Armoury"):
    s = rules.find_slot(st, b)
    if s:
        print(b, "upgrades:", c.upgrades(s))
th = rules.find_slot(st, "Town Hall")
if th:
    print("celebration running:", c.celebration_running(th))
print("troops:", st["troops"])
# travel time probe: reinforcement of 1 basic defender to the nearest other own village (not sent)
vs = [v for v in st["villages"] if v.get("x") is not None]
if len(vs) > 1:
    t = vs[1]
    r = c.send_army(t["x"], t["y"], 2, {1: 1}, probe=True)
    print("probe to", t["name"], r)
reps = c.reports()
print("reports:", reps[:5])
for rep in reps[:3]:
    det = c.report_detail(rep["id"])
    print("  ", rep["topic"], "->", {k: det.get(k) for k in ("attacker_uid", "attacker_wref", "target", "sent", "def_troops", "info")} if det else None)
