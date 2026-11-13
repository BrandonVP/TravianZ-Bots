import json, pathlib, sys, re
from travbot.client import GameClient
from travbot import rules, parse
d = pathlib.Path(sys.argv[1]); acc = json.loads((d / "bot-account.json").read_text())
c = GameClient("http://192.168.68.205/dev", acc["user"], acc["pw"]); c.login()
st = c.village(); slot = rules.find_slot(st, "Marketplace")
html = c.get(f"build.php?id={slot}")
print("market:", parse.parse_market(html))
soup = parse._soup(html)
for p in soup.find_all(["p", "div", "td", "span"]):
    t = p.get_text(" ", strip=True)
    if re.search(r"carr|merchant|cart", t, re.I) and len(t) < 160:
        print(" ", p.name, p.get("class"), "|", t)
