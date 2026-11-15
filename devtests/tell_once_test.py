"""Hostiles.tell_once(): a victim is told once that an attacker was answered, and again only after a new attack."""
import pathlib, tempfile
from travbot.intel import Hostiles

d = pathlib.Path(tempfile.mkdtemp()) / "state"
d.mkdir()
h = Hostiles(d)
feed = lambda move: {"alliance": 1, "members": [{"uid": 6, "name": "Brandon"}],
                     "incoming": [{"from_owner": 9, "from_name": "hun", "from": 79602, "from_x": 3, "from_y": 2,
                                   "moveid": move, "to_owner": 6, "kind": "raid"}]}
h.observe(feed(1), now=1000)
assert h.tell_once(9, "Brandon", now=2000) is True
assert h.tell_once(9, "Brandon", now=3000) is False      # second and third strike: no letter
assert h.tell_once(9, "Brandon", now=90000) is False
assert h.tell_once(9, "Abi", now=3000) is True           # every victim gets their own
h.observe(feed(1), now=4000)                              # the same movement seen again is not a new attack
assert h.tell_once(9, "Brandon", now=5000) is False
h.observe(feed(2), now=6000)                              # a new attack
assert h.tell_once(9, "Brandon", now=7000) is True
assert h.tell_once(9, "Brandon", now=8000) is False
assert h.tell_once(77, "Brandon") is False                # unknown player
print("tell_once_test: ok")
