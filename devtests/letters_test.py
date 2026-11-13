"""Dry-run test of letters -> requests -> reply on the DEV game (nothing is sent): python -m devtests.letters_test state-teuton"""
import json, logging, os, pathlib, sys
logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
from travbot.client import GameClient
from travbot import plan as planmod
from travbot.main import Bot
from travbot.diplomacy import Diplomacy, template_letter
from travbot.letters import Requests
from travbot.mailer import Mailer
d = pathlib.Path(sys.argv[1]).resolve()
acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"])
bot = Bot(c, int(acc["tribe"]), d, 6, True, Diplomacy(d, acc["user"], lambda k, ctx: template_letter(k, ctx)))
plan, src = planmod.load(d / "plan.json", 1e9)
bot.tick(plan)
bot.humans.refresh(c)
url, models = os.environ["TRAVBOT_OLLAMA_URL"], ["gemma4:26b", "qwen3:14b"]
req = Requests(bot, url, models)
m = Mailer(c, acc["user"], int(acc["tribe"]), d, url, models, [])
br = next(mm for mm in bot.humans.ally["members"] if mm["name"] == "Brandon")
bv = min(br["villages"], key=lambda v: v["pop"])
me = bot.last_seen[0]; mc = bot.coords.get(me["id"])
letters = [
    ("Clay please", f"Hi! My little village {bv['name']} at ({bv['x']}|{bv['y']}) is short on clay, could you send some?"),
    ("Help!", f"Someone is about to hit my village at ({bv['x']}|{bv['y']}). Can you send defenders so they land at 21:30?"),
    ("Take them down", f"Please attack the village at ({mc[0] + 3}|{mc[1] - 2}), I don't like that guy."),
    ("Hello", "Hi neighbour, how is your day going? Any tips for a new player?"),
]
for subj, body in letters:
    print("\n=== LETTER:", subj, "|", body)
    reqs = req.read("Brandon", subj, body)
    print("read:", reqs)
    acts = req.handle("Brandon", reqs or [])
    print("did:", acts)
    print("reply:", m._write(f'Brandon wrote you a letter with the subject "{subj}":\n---\n{body}\n---\nWrite your reply to Brandon.', acts))
