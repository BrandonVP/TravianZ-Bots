"""Dry-run test of the hostile list and counter-strike decisions on the DEV game (nothing is sent)."""
import json, logging, math, pathlib, sys, time
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
from travbot.client import GameClient, tile_id
from travbot import plan as planmod, combat, rules
from travbot.main import Bot
from travbot.diplomacy import Diplomacy, template_letter
d = pathlib.Path(sys.argv[1]).resolve()
acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"])
bot = Bot(c, int(acc["tribe"]), d, 6, True, Diplomacy(d, acc["user"], lambda k, ctx: template_letter(k, ctx)))
plan, _ = planmod.load(d / "plan.json", 1e9)
plan = planmod.validate({})   # defaults (incl. counter) for a clean test
bot.tick({**plan})
bot.humans.refresh(c)
members = {m["name"] for m in bot.humans.ally["members"]}
army_v = next(v for v in bot.last_seen if bot.role_of.get(v["id"]) == "army")
ac = bot.coords[army_v["id"]]
cands = [(math.hypot(v["x"] - ac[0], v["y"] - ac[1]), k, v) for k, v in bot.raid.data["villages"].items()
         if v.get("owner") and v["owner"] not in members and v["owner"] not in plan["raid"]["exclude"]
         and v["owner"] not in plan["raid"]["farm_accounts"]]
dist, wref, tv = min(cands)
print(f"army village {army_v['name']} {ac}; test target {tv['owner']}'s {tv['name']} ({tv['x']}|{tv['y']}) pop {tv['pop']} at {dist:.1f}")
me_uid = next(m["uid"] for m in bot.humans.ally["members"] if m["name"] == acc["user"])
fake = {"alliance": {"id": 1}, "members": bot.humans.ally["members"], "incoming": [{
    "moveid": 990001, "to": tile_id(*ac), "to_owner": me_uid, "from": int(wref), "from_owner": 99999,
    "from_name": tv["owner"], "from_x": tv["x"], "from_y": tv["y"], "kind": "raid", "arrives": int(time.time()) + 600}]}
print("observe:", bot.hostiles.observe(fake))
bot.hostiles.note_force(99999, {combat.offence_units(bot.tribe)[0]: 40})
st = army_v["state"]
scout = combat.SCOUTS[bot.tribe]; off = combat.offence_units(bot.tribe)[0]
pop = {int(k): v.get("pop") for k, v in bot.raid.data["villages"].items()}
def run(label, troops, scouted=None, popmap=pop):
    st["troops"] = dict(troops)
    bot.army.data["scouting"].clear(); bot.army.data["skip"].clear(); bot.army.data["scouted"].clear()
    if scouted is not None:
        bot.army.data["scouted"][str(wref)] = {"time": time.time(), "def_troops": scouted, "info": ""}
    acts = bot.army.strikes(bot.last_seen, bot.role_of, bot.coords, plan, acc["user"], members, popmap)
    print(f"--- {label}: {[a['name'] for a in acts]}  log: {[e['text'] for e in bot.army.data['log'][-1:]]}")
run("A no scouting yet", {scout: 5, off: 200})
run("B weak target", {scout: 5, off: 200}, {rules.basic_defender(bot.tribe): 20})
run("C strong target", {scout: 5, off: 200}, {rules.basic_defender(bot.tribe): 2000})
run("D tiny village", {scout: 5, off: 200}, {}, {int(wref): 30})
bot.hostiles.note_strike(99999, acc["user"], int(wref), {off: 10})
run("E already struck today", {scout: 5, off: 200}, {})
# clean up the dev hostile list
p = d.parent / "hostiles.json"; h = json.loads(p.read_text()); h["players"].pop("99999", None)
h["strikes"] = [s for s in h["strikes"] if s["uid"] != 99999]; h["moves"].pop("990001", None); p.write_text(json.dumps(h))
