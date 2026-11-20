"""Side-by-side test of the AI calls through travbot/llm.py on the DEV game (dry run, nothing is sent or saved):
   TRAVBOT_TEST_MODELS=strata,gemma4:26b python -m devtests.strata_test state-teuton
Per model: an advisor plan from the bot's real status, an AI letter (invite), and two members' letters read into
requests plus the reply written for one. Prints timings and results; the plan goes through plan.validate."""
import json, logging, os, pathlib, sys, time
logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
from travbot.client import GameClient
from travbot import plan as planmod
from travbot.main import Bot, _alliance_summary
from travbot.advisor import Advisor
from travbot.diplomacy import Diplomacy, template_letter
from travbot.letters import Requests
from travbot.mailer import Mailer

d = pathlib.Path(sys.argv[1]).resolve()
acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"])
bot = Bot(c, int(acc["tribe"]), d, 6, True, Diplomacy(d, acc["user"], lambda k, ctx: template_letter(k, ctx)))
plan, src = planmod.load(d / "plan.json", 1e9)
villages, actions, _ = bot.tick(plan)
bot.humans.refresh(c)
status = {"account": acc["user"], "plan_source": src, "tribe": bot.tribe, "villages": villages,
          "expansion": bot.expansion.summary(), "raid": bot.raid.summary(),
          "world": {"day": bot.day, "phase": bot.phase, "relics_day": bot.world.info()["relics_day"],
                    "wonder_day": bot.world.info()["wonder_day"]},
          "hostiles": bot.hostiles.summary(plan["counter"]["hostile_days"]), "army": bot.army.summary(),
          "champions": bot.champions.summary(), "alliance": _alliance_summary(bot.humans.ally),
          "recent_actions": actions}
url = os.environ.get("TRAVBOT_OLLAMA_URL", "http://192.168.68.204:30068")
br = next(mm for mm in bot.humans.ally["members"] if mm["name"] == "Brandon")
bv = min(br["villages"], key=lambda v: v["pop"])
letters = [("Clay please", f"Hi! My little village {bv['name']} at ({bv['x']}|{bv['y']}) is short on clay, could you send some?"),
           ("Help!", f"Someone is about to hit my village at ({bv['x']}|{bv['y']}). Can you send defenders so they land at 21:30?")]

for model in [m.strip() for m in os.environ.get("TRAVBOT_TEST_MODELS", "strata").split(",") if m.strip()]:
    print(f"\n######## {model}")
    adv = Advisor(url, "", [model], d, 0, 0, tribe=bot.tribe, yield_to_others=False)
    t = time.time()
    try:
        raw = adv._ask(model, status, plan)
        clean = planmod.validate(raw)
        print(f"PLAN {time.time() - t:.0f}s, keys {sorted(clean)}\n  notes: {str(raw.get('notes', ''))[:400]}")
        print("  build/troops:", json.dumps({k: clean[k] for k in ("build", "troops") if k in clean})[:600])
    except Exception as e:
        print(f"PLAN failed after {time.time() - t:.0f}s: {type(e).__name__}: {e}")
    t = time.time()
    out = adv.letter("invite", {"me": acc["user"], "alliance": "Legio Test", "tag": "LGT", "to": "Brandon"})
    print(f"LETTER {time.time() - t:.0f}s: {out}")
    req = Requests(bot, url, [model])
    m = Mailer(c, acc["user"], int(acc["tribe"]), d, url, [model], [])
    for subj, body in letters:
        t = time.time()
        reqs = req.read("Brandon", subj, body)
        print(f"READ {time.time() - t:.0f}s '{subj}': {reqs}")
    t = time.time()
    reply = m._write(f'Brandon wrote you a letter with the subject "Hello":\n---\nHi neighbour, any tips for a new player?\n---\n'
                     f'Write your reply to Brandon.', [])
    print(f"REPLY {time.time() - t:.0f}s: {reply}")
