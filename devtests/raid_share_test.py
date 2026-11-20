"""Check of the shared raid record (travbot/raid.py): python -m devtests.raid_share_test (no game access)"""
import pathlib, tempfile, time
from travbot import raid
from travbot.plan import DEFAULT_PLAN as DEFAULTS
cfg = dict(DEFAULTS["raid"])
now = time.time()
tmp = pathlib.Path(tempfile.mkdtemp())
(tmp / "a").mkdir(); (tmp / "b").mkdir()
a, b = raid.RaidBook(tmp / "a"), raid.RaidBook(tmp / "b")
vill = {"x": 3, "y": 2, "owner": "hun", "name": "hun's village", "pop": 100, "since": now - 10 * 86400, "tribe": 1}
for bk in (a, b):
    bk.data["villages"]["79602"] = dict(vill)
    bk.data["targets"]["79602"] = {"scouted": now - 3600, "defended": False}
kind = a.kind_of("79602", cfg, "AIBot", set(), None, now)
print("kind:", kind)
before_b = [t["key"] for t in b.plan_raids((0, 0), cfg, "AIRoman", set(), None, now)]
a.sent(1, "79602", "raid", 20, now)
after_b = [t["key"] for t in b.plan_raids((0, 0), cfg, "AIRoman", set(), None, now + 60)]
later_b = [t["key"] for t in b.plan_raids((0, 0), cfg, "AIRoman", set(), None, now + cfg["cooldown_hours"] * 3600 + 120)]
print("b before:", before_b, "| b right after a raided:", after_b, "| b after the cooldown:", later_b)
assert "79602" in before_b and "79602" not in after_b and "79602" in later_b
rep = {"target": 79602, "sent": {21: 20}, "dead": {}, "loot": 20, "carry": 1000, "defenders": 0}
print(a.learn(rep, [21], 23, cfg, now + 1800))
wait = (a.data["targets"]["79602"]["next"] - (now + 1800)) / 3600
print(f"a waits {wait:.1f} h after a 2% haul")
assert wait >= 24
print("OK")
