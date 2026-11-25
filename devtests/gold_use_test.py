"""Real purchase test on the DEV game only: python devtests/gold_use_test.py state-roman
Buys the production bonus the rules pick (1,000 dev gold) and makes one NPC trade (250), checking gold and result."""
import json, pathlib, sys
from travbot.client import GameClient
from travbot import rules, parse, plan as planmod
d = pathlib.Path(sys.argv[1]); acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"]); c.login()
c.select_village(c.capital_id())
st = c.village(); pl = planmod.validate({})
b = c.bonuses(); print("before:", b["left"], "gold", st["gold"]["amount"])
pick = rules.bonus_choice(st, pl, b["left"], st["gold"]); print("pick:", pick)
if pick:
    after = c.buy_bonus(pick[0], b["ids"]); st2 = c.village()
    print("after:", after["left"], "gold", st2["gold"]["amount"],
          "OK" if after["left"][pick[0]] > b["left"].get(pick[0], 0) and st["gold"]["amount"] - st2["gold"]["amount"] >= 999 else "FAILED")
st = c.village(); slot = rules.find_slot(st, "Marketplace"); form = c.npc_form(slot)
snap = {k: {"amount": n} for k, n in zip(("wood", "clay", "iron", "crop"), form["m1"])}
caps = {k: st["resources"][k]["cap"] for k in snap}
cost = {"wood": 1000, "clay": 2000, "iron": 500, "crop": 100}
target = rules.npc_split(cost, snap, caps); print("npc", form["m1"], "->", target, "caps", caps)
c.npc_trade(slot, form["m1"], target); st3 = c.village()
print("npc done: gold", st["gold"]["amount"], "->", st3["gold"]["amount"], "url", c.last_url,
      "OK" if st["gold"]["amount"] - st3["gold"]["amount"] >= 249 else "FAILED")
