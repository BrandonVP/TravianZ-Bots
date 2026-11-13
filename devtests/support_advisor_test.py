"""Dry-run: Wonder/relic support with a simulated alliance Wonder, then the new advisor prompt (read-only)."""
import copy, json, logging, os, pathlib, sys, time
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
from travbot.client import GameClient
from travbot import plan as planmod
from travbot.main import Bot, _alliance_summary
from travbot.advisor import Advisor
from travbot.diplomacy import Diplomacy, template_letter
d = pathlib.Path(sys.argv[1]).resolve()
acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"])
bot = Bot(c, int(acc["tribe"]), d, 6, True, Diplomacy(d, acc["user"], lambda k, ctx: template_letter(k, ctx)))
plan = planmod.validate({})
bot.tick({**plan})
bot.humans.refresh(c)
feed = copy.deepcopy(bot.humans.ally)
br = next(m for m in feed["members"] if m["name"] == "Brandon")
br["villages"][0].update(ww=True, ww_level=12)
br["villages"][1]["relics"] = [{"type": 2, "size": 1, "active": True, "name": "test relic"}]
print("wonders:", [(v["name"], v["x"], v["y"]) for v in br["villages"] if v.get("ww")])
for a in bot.support.step(bot.last_seen, bot.coords, plan, feed, natars_attacking=True):
    print("  support:", a["village"], "->", a["name"], "|", a["reason"])
status = {"villages": [], "world": {"day": bot.day, "phase": bot.phase, "relics_day": 30, "wonder_day": 50},
          "hostiles": ["hun (uid 99999): 2 attack(s) on AIRoman x2"], "army": bot.army.summary(),
          "alliance": _alliance_summary(feed), "expansion": None, "recent_actions": []}
st = json.loads((d / "status.json").read_text()) if (d / "status.json").exists() else None
live = json.loads(open("/live-status.json").read()) if os.path.exists("/live-status.json") else None
status["villages"] = (live or {}).get("villages") or []
for v in status["villages"]:
    v["role"] = "feeder"
adv = Advisor(os.environ["TRAVBOT_OLLAMA_URL"], "", ["gemma4:26b"], d, 0, 0, timeout_s=900, tribe=bot.tribe)
t0 = time.time()
raw = adv._ask("gemma4:26b", status, plan)
clean = planmod.validate(raw)
print(f"advisor: {time.time() - t0:.0f}s notes: {raw.get('notes')}")
print("army:", clean["army"], "culture:", clean["culture"], "wonder:", clean["wonder"])
print("targets:", [(t["name"], t["level"]) for t in clean["building_targets"]])
