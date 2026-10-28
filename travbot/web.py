"""Read-only status pages: / (first bot), /b/<name> (other bots), plus /status.json and /plan.json
(or /b/<name>/status.json ...). HTML refreshes every minute."""
import html
import json
import logging
import threading
import time
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

log = logging.getLogger(__name__)

CSS = """
:root { --bg:#f6f7f9; --card:#fff; --text:#1d2330; --muted:#667085; --line:#e4e7ec;
        --accent:#2f6fde; --warn:#c2410c; --ok:#15803d; --bar:#dbe4f5; }
@media (prefers-color-scheme: dark) {
  :root { --bg:#12151b; --card:#1b2029; --text:#e6e9ef; --muted:#98a2b3; --line:#2a313d;
          --accent:#6ea0ff; --warn:#fb923c; --ok:#4ade80; --bar:#26324a; } }
* { box-sizing:border-box } body { margin:0; background:var(--bg); color:var(--text);
  font:14px/1.45 system-ui,-apple-system,Segoe UI,Roboto,sans-serif; }
main { max-width:1100px; margin:0 auto; padding:16px; }
h1 { font-size:20px; margin:4px 0 2px } h2 { font-size:16px; margin:0 0 8px }
h3 { font-size:13px; margin:12px 0 4px; color:var(--muted); text-transform:uppercase; letter-spacing:.04em }
.sub { color:var(--muted); margin-bottom:14px }
.grid { display:grid; gap:14px; grid-template-columns:repeat(auto-fit,minmax(320px,1fr)) }
.card { background:var(--card); border:1px solid var(--line); border-radius:10px; padding:14px }
.res { display:grid; grid-template-columns:52px 1fr auto; gap:4px 8px; align-items:center }
.bar { height:8px; background:var(--bar); border-radius:4px; overflow:hidden }
.bar i { display:block; height:100%; background:var(--accent) }
.num { font-variant-numeric:tabular-nums; color:var(--muted); font-size:12px; text-align:right }
ul { margin:0; padding-left:18px } li { margin:2px 0 }
.warn { color:var(--warn); font-weight:600 } .ok { color:var(--ok) } .muted { color:var(--muted) }
.notes { white-space:pre-wrap; background:var(--bg); border-radius:6px; padding:8px; margin-top:6px }
pre { white-space:pre-wrap; word-break:break-word; font-size:12px; margin:0; max-height:420px; overflow:auto }
table { border-collapse:collapse; width:100% } td { padding:2px 4px; border-bottom:1px solid var(--line) }
"""


def _read_json(path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _ago(ts):
    if not ts:
        return "never"
    s = int(time.time() - ts)
    if s < 120:
        return f"{s}s ago"
    if s < 7200:
        return f"{s // 60} min ago"
    return f"{s / 3600:.1f} h ago"


def _tail(path, n):
    try:
        with path.open(encoding="utf-8", errors="replace") as f:
            return list(deque(f, maxlen=n))
    except OSError:
        return []


def _village_card(v):
    e = html.escape
    rows = []
    for k in ("wood", "clay", "iron", "crop"):
        r = v["resources"][k]
        pct = min(100, 100 * r["amount"] / r["cap"]) if r["cap"] else 0
        label = f'{r["amount"]:,} / {r["cap"]:,} &middot; {r["prod"]:+}/h'
        rows.append(f'<span>{k}</span><div class="bar"><i style="width:{pct:.0f}%"></i></div>'
                    f'<span class="num">{label}</span>')
    inc = v["incoming"]
    attack = (f'<p class="warn">&#9888; {inc["attacks"]} attack(s) incoming, first in '
              f'{(inc["first_in"] or 0) // 60} min</p>' if inc["attacks"] else "")
    queue = "".join(f'<li>{e(q["name"])} &rarr; {q["to_level"]} <span class="muted">({q["seconds_left"] // 60} min)</span></li>'
                    for q in v["queue"]) or '<li class="muted">empty</li>'
    troops = "".join(f"<li>{n} {e(name)}</li>" for name, n in v["troops"].items()) or '<li class="muted">none</li>'
    blds = ", ".join(f'{e(b["name"])} {b["level"]}' for b in v["buildings"].values()) or "none"
    levels = {}
    for f in v["fields"]:
        levels.setdefault(f["kind"], []).append(f["level"])
    fields = " &middot; ".join(f"{k} {sorted(l)}" for k, l in sorted(levels.items()))
    saving = (f'<p>Saving for <b>{e(v["saving_for"]["name"])}</b> (~{v["saving_for"]["eta_min"]} min)</p>'
              if v.get("saving_for") else "")
    coords = f' <span class="muted">({v["coords"][0]}|{v["coords"][1]})</span>' if v.get("coords") else ""
    note = f'<p class="muted">Expansion: {e(v["expansion_note"])}</p>' if v.get("expansion_note") else ""
    return (f'<div class="card"><h2>{e(v["name"])}{coords}</h2>{attack}<div class="res">{"".join(rows)}</div>'
            f'{saving}<h3>Build queue</h3><ul>{queue}</ul><h3>Troops</h3><ul>{troops}</ul>'
            f'<h3>Buildings</h3><div>{blds}</div><h3>Fields</h3><div class="muted">{fields}</div>{note}</div>')


def _diplomacy_card(d):
    if not d or d.get("role") in (None, "none"):
        return ""
    e = html.escape
    rows = [f'<p>Role: <b>{e(d["role"])}</b> &middot; alliance: <b>{e(d.get("alliance") or "none yet")}</b></p>']
    if d.get("members"):
        rows.append(f'<p>Members: {e(", ".join(d["members"]))}</p>')
    if d.get("invited"):
        rows.append(f'<p class="muted">Invited: {e(", ".join(d["invited"]))}</p>')
    return f'<div class="card"><h2>Alliance</h2>{"".join(rows)}</div>'


def _raid_html(r):
    """Raids summary (travbot/raid.py RaidBook.summary)."""
    e = html.escape
    if not r:
        return '<p class="muted">no raids yet</p>'
    days = "".join(f"<li>{e(d)}: {t['raids']} raids, {t['loot']:,} resources</li>" for d, t in r.get("totals", {}).items())
    return (f'<p>{r.get("out", 0)} under way &middot; {r.get("known_villages", 0)} villages and '
            f'{r.get("empty_oases", 0)} empty oases known &middot; {r.get("blocked", 0)} targets blocked</p>'
            f'<ul>{days or "<li class=muted>no loot yet</li>"}</ul>')


def render(state_dir, name="AIBot", nav=""):
    e = html.escape
    status = _read_json(state_dir / "status.json")
    plan = _read_json(state_dir / "plan.json") or {}
    adv = _read_json(state_dir / "advisor.json") or {}
    if not status or "villages" not in status:
        body = '<p class="muted">No status yet - the bot has not finished its first round.</p>'
    else:
        villages = "".join(_village_card(v) for v in status["villages"])
        actions = "".join(
            f'<tr><td class="muted">{time.strftime("%H:%M", time.localtime(a["time"]))}</td>'
            f'<td>{e(a.get("village", ""))}</td><td>{e(a["action"])} {e(a["name"])}'
            f'{" x" + str(a["count"]) if a.get("count") else ""}</td><td class="muted">{e(a.get("reason", ""))}</td></tr>'
            for a in reversed(status["recent_actions"][-20:])) or '<tr><td class="muted">none yet</td></tr>'
        from .names import display
        targets = ", ".join(f'{e(display(t["name"]))} {t["level"]}' for t in plan.get("building_targets", [])) or "default"
        exp = status.get("expansion") or {}
        exp_html = "".join(f'<li>village {e(str(k))}: scanned radius {x["radius"]}, '
                           f'{x["valleys_found"]} free valleys{", settlers sent" if x.get("sent") else ""}</li>'
                           for k, x in exp.items()) or '<li class="muted">not started (needs Residence 10)</li>'
        body = (
            f'<div class="sub">Updated {_ago(status["updated"])} &middot; next round in ~{status["next_tick_in"] // 60} min'
            f' &middot; plan: {e(status["plan_source"])}</div>'
            f'<div class="grid">{villages}</div><div class="grid" style="margin-top:14px">'
            f'<div class="card"><h2>AI advisor</h2><p>Last plan {_ago(adv.get("last_success"))}'
            f'{" by " + e(adv["last_model"]) if adv.get("last_model") else ""}; last attempt {_ago(adv.get("last_attempt"))}.</p>'
            f'<div class="notes">{e(plan.get("notes") or "No AI notes yet.")}</div>'
            f'<h3>Building targets</h3><div>{targets}</div><h3>Expansion</h3><ul>{exp_html}</ul>'
            f'<h3>Raids</h3>{_raid_html(status.get("raid"))}</div>'
            f'<div class="card"><h2>Recent actions</h2><table>{actions}</table></div>'
            f'{_diplomacy_card(status.get("diplomacy"))}</div>'
        )
    logs = e("".join(_tail(state_dir / "bot.log", 40)))
    return (f'<!doctype html><html lang="en"><head><meta charset="utf-8">'
            f'<meta name="viewport" content="width=device-width,initial-scale=1">'
            f'<meta http-equiv="refresh" content="60"><title>{e(name)} Status</title><style>{CSS}</style></head>'
            f'<body><main>{nav}<h1>{e(name)} status</h1>{body}'
            f'<div class="card" style="margin-top:14px"><h2>Log</h2><pre>{logs}</pre></div></main></body></html>')


def serve(bots, port):
    """bots: [(name, state_dir)]; the first one is shown at /."""
    by_name = {n.lower(): (n, d) for n, d in bots}

    def nav(current):
        links = []
        for i, (n, _d) in enumerate(bots):
            href = "/" if i == 0 else f"/b/{n}"
            style = "font-weight:700" if n == current else ""
            links.append(f'<a href="{href}" style="{style}">{html.escape(n)}</a>')
        return f'<div class="sub">Bots: {" &middot; ".join(links)}</div>' if len(bots) > 1 else ""

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _send(self, body, ctype):
            data = body.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):
            path = self.path.split("?")[0]
            name, state_dir = bots[0]
            if path.startswith("/b/"):
                parts = path[3:].split("/", 1)
                if parts[0].lower() not in by_name:
                    self.send_error(404)
                    return
                name, state_dir = by_name[parts[0].lower()]
                path = "/" + (parts[1] if len(parts) > 1 else "")
            if path in ("/status.json", "/plan.json"):
                data = _read_json(state_dir / path.lstrip("/"))
                self._send(json.dumps(data, indent=2), "application/json")
            elif path in ("/", "/index.html"):
                try:
                    self._send(render(state_dir, name, nav(name)), "text/html; charset=utf-8")
                except Exception as exc:  # never let the page take the bot down
                    log.exception("status page error")
                    self._send(f"status page error: {html.escape(str(exc))}", "text/plain")
            else:
                self.send_error(404)

    server = ThreadingHTTPServer(("0.0.0.0", port), Handler)
    threading.Thread(target=server.serve_forever, daemon=True, name="web").start()
    log.info("status page on port %d", port)
