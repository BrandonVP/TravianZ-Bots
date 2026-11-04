"""Alliance building between the bots, through the normal Embassy and alliance pages.

Configured per bot in state/diplomacy.json (kept apart from plan.json, which the AI advisor rewrites):

    {"role": "founder", "tag": "ACCORD", "name": "The Accord",
     "invite": ["AIRoman", "AITeuton"]}                     # AIBot
    {"role": "member", "accept_from": ["The Accord", "ACCORD"],
     "founder": "AIBot"}                                     # the other bots
    {"role": "founder", "tag": "ACCORD", "name": "The Accord", "invite": [...],
     "half": "north", "recruit_per_day": 5}                  # a half's founder also recruits human players

founder: with an Embassy at level 3 it founds the alliance, then invites each player on its list (again after a
         day if they still have not joined), sending each a personal message.
         With "half": "north" or "south" it also recruits human players who start in that half of the map
         (plan 2026-10-29-ai-allies-for-players): active, in no alliance, a few hours after they registered; at most
         "recruit_per_day" invitations a day, each player at most twice, "reinvite_days" apart, never past the
         alliance's member limit. New human members get a welcome letter. Letters are templates; real questions
         are answered by hand (mail/mailbot.py) - the bot never reads or marks its inbox.
member:  with an Embassy at level 1 it accepts an invitation from an alliance on its accept_from list and thanks
         the founder. Until then it waits - the building targets in its plan get the Embassy built.

Runtime progress lives in state/diplomacy_state.json. The game decides everything; the bot only uses its pages.
"""
import json
import logging
import math
import time

from . import parse, rules

log = logging.getLogger(__name__)

REINVITE_S = 24 * 3600
RECRUIT_DEFAULTS = {"recruit_after_h": 3, "recruit_per_day": 5, "reinvite_days": 7, "max_invites": 2}


def _load(path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def _save(path, data):
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(path)


class Diplomacy:
    def __init__(self, state_dir, me, writer=None):
        self.config_path = state_dir / "diplomacy.json"
        self.state_path = state_dir / "diplomacy_state.json"
        self.me = me
        # writer(kind, context) -> (subject, text): letters, AI-written when the advisor PC is up
        self.writer = writer or template_letter

    def config(self):
        return _load(self.config_path, {"role": "none"})

    def summary(self):
        st = _load(self.state_path, {})
        return {"role": self.config().get("role", "none"), "alliance": st.get("alliance"),
                "invited": sorted(st.get("invited", {})), "members": st.get("members", [])}

    def step(self, client, villages, dry_run=False):
        """One diplomacy step per round. `villages`: [{"id", "name", "state"}]. Returns a log line or None."""
        cfg = self.config()
        role = cfg.get("role", "none")
        if role not in ("founder", "member"):
            return None
        # the Embassy with the highest level among the bot's villages
        best = None
        for v in villages:
            slot = rules.find_slot(v["state"], "Embassy")
            if slot:
                lvl = rules.building_level(v["state"], "Embassy")
                if best is None or lvl > best[2]:
                    best = (v, slot, lvl)
        if best is None:
            return None
        v, slot, level = best
        st = _load(self.state_path, {"alliance": None, "invited": {}, "members": [], "letters": []})
        client.select_village(v["id"]) if v.get("id") is not None else None
        page = parse.parse_embassy(client.get(f"build.php?id={slot}"))
        st["alliance"] = page["alliance"]
        if role == "founder":
            msg = self._founder(client, cfg, st, page, slot, level, dry_run)
        else:
            msg = self._member(client, cfg, st, page, slot, dry_run)
        _save(self.state_path, st)
        return msg

    # --- founder ---------------------------------------------------------------

    def _founder(self, client, cfg, st, page, slot, level, dry_run):
        if not page["alliance"]:
            if level < 3:
                return None  # the plan builds the Embassy up to 3 first
            if dry_run:
                return f"[dry-run] would found the alliance [{cfg['tag']}] {cfg['name']}"
            client.post("build.php", {"ft": "ali1", "id": slot, "ally1": cfg["tag"], "ally2": cfg["name"], "s1": "ok"})
            page = parse.parse_embassy(client.get(f"build.php?id={slot}"))
            st["alliance"] = page["alliance"]
            if not page["alliance"]:
                return "could not found the alliance yet (name or tag taken?)"
            return f"founded the alliance [{cfg['tag']}] {cfg['name']}"

        members = parse.parse_alliance_members(client.get("allianz.php"))
        st["members"] = members
        now = time.time()
        for name in cfg.get("invite", []):
            if name in members:
                continue
            last = st["invited"].get(name, 0)
            if now - last < REINVITE_S:
                continue
            if dry_run:
                return f"[dry-run] would invite {name} to {cfg['name']}"
            html = client.post("allianz.php", {"s": 5, "o": 4, "a": 4, "a_name": name, "s1": "ok"})
            err = parse.parse_form_error(html)
            st["invited"][name] = now
            if err and "already invited" not in err.lower():
                return f"invitation to {name} refused: {err}"
            subject, text = self.writer("invite", {"me": self.me, "to": name, "alliance": cfg["name"],
                                                   "tag": cfg["tag"], "members": members})
            client.send_message(name, subject, text)
            st["letters"] = (st.get("letters", []) + [{"to": name, "subject": subject, "time": int(now)}])[-20:]
            return f"invited {name} to [{cfg['tag']}] {cfg['name']} and wrote: {subject}"
        if cfg.get("half") in ("north", "south"):
            return self._humans(client, cfg, st, dry_run)
        return None

    # --- human players (founder of a half's alliance) -------------------------

    def _humans(self, client, cfg, st, dry_run):
        """Welcome new human members, then invite at most one human player per round."""
        ally = json.loads(client.get("ajax.php?f=tzbot&a=ally"))
        if not ally.get("alliance"):
            return None
        now = time.time()
        humans = [m for m in ally["members"] if not m["bot"]]
        bot_villages = [(v, m["name"]) for m in ally["members"] if m["bot"] for v in m["villages"]]
        welcomed = st.setdefault("welcomed", None)
        if welcomed is None:
            # first run: the humans already in the alliance get no welcome letter
            st["welcomed"] = [m["uid"] for m in humans]
            welcomed = st["welcomed"]
        for m in humans:
            if m["uid"] in welcomed:
                continue
            cap = next((v for v in m["villages"] if v["capital"]), (m["villages"] or [None])[0])
            near = _nearest(cap, bot_villages) if cap else None
            ctx = {"me": self.me, "to": m["name"], "alliance": cfg["name"], "tag": cfg["tag"],
                   "village": cap["name"] if cap else "", "near": near}
            if dry_run:
                return f"[dry-run] would welcome {m['name']} to {cfg['name']}"
            subject, text = template_letter("welcome", ctx)
            client.send_message(m["name"], subject, text)
            welcomed.append(m["uid"])
            st["letters"] = (st.get("letters", []) + [{"to": m["name"], "subject": subject, "time": int(now)}])[-20:]
            return f"welcomed {m['name']} to [{cfg['tag']}] {cfg['name']}"

        opt = dict(RECRUIT_DEFAULTS, **{k: cfg[k] for k in RECRUIT_DEFAULTS if k in cfg})
        if ally["alliance"]["max"] and len(ally["members"]) >= ally["alliance"]["max"]:
            return None  # the Embassy is full; it grows with the plan
        rec = st.setdefault("recruits", {})
        today = [t for r in rec.values() for t in r.get("times", []) if now - t < 86400]
        if len(today) >= opt["recruit_per_day"]:
            return None
        member_ids = {m["uid"] for m in ally["members"]}
        for p in json.loads(client.get("ajax.php?f=tzbot&a=recruits")).get("players", []):
            if p["half"] != cfg["half"] or p["uid"] in member_ids:
                continue
            if now - p["registered"] < opt["recruit_after_h"] * 3600:
                continue
            r = rec.get(str(p["uid"]), {"times": []})
            if len(r["times"]) >= opt["max_invites"] or (r["times"] and now - r["times"][-1] < opt["reinvite_days"] * 86400):
                continue
            near = _nearest(p["capital"], bot_villages)
            if dry_run:
                return f"[dry-run] would invite {p['name']} ({p['capital']['x']}|{p['capital']['y']}) to {cfg['name']}"
            html = client.post("allianz.php", {"s": 5, "o": 4, "a": 4, "a_name": p["name"], "s1": "ok"})
            err = parse.parse_form_error(html)
            r["times"].append(now)
            rec[str(p["uid"])] = r
            if err and "already invited" not in err.lower():
                return f"invitation to {p['name']} refused: {err}"
            ctx = {"me": self.me, "to": p["name"], "alliance": cfg["name"], "tag": cfg["tag"],
                   "village": p["capital"]["name"], "near": near, "again": len(r["times"]) > 1}
            subject, text = template_letter("invite_player", ctx)
            client.send_message(p["name"], subject, text)
            st["letters"] = (st.get("letters", []) + [{"to": p["name"], "subject": subject, "time": int(now)}])[-20:]
            return f"invited {p['name']} to [{cfg['tag']}] {cfg['name']} and wrote: {subject}"
        return None

    # --- member ----------------------------------------------------------------

    def _member(self, client, cfg, st, page, slot, dry_run):
        if page["alliance"]:
            return None
        wanted = {n.lower() for n in cfg.get("accept_from", [])}
        for inv in page["invites"]:
            if inv["alliance"].lower() not in wanted:
                continue
            if dry_run:
                return f"[dry-run] would accept the invitation from {inv['alliance']}"
            client.get(inv["accept_href"])
            page = parse.parse_embassy(client.get(f"build.php?id={slot}"))
            st["alliance"] = page["alliance"]
            if not page["alliance"]:
                return f"could not accept the invitation from {inv['alliance']} yet"
            founder = cfg.get("founder")
            if founder:
                subject, text = self.writer("accept", {"me": self.me, "to": founder, "alliance": inv["alliance"]})
                client.send_message(founder, subject, text)
            return f"joined {inv['alliance']}"
        return None


def template_letter(kind, ctx):
    """Plain letters for when the AI writer is unavailable."""
    if kind == "invite":
        return (f"An alliance of neighbours",
                f"Greetings {ctx['to']},\n\nOur villages grow side by side, and the land around us is wide. "
                f"I have founded {ctx['alliance']} [{ctx['tag']}] so that neighbours can stand together: "
                f"we share surplus, warn each other of raiders and grow stronger as one.\n\n"
                f"An invitation is waiting for you at your Embassy. You would be most welcome.\n\n{ctx['me']}")
    if kind == "accept":
        return (f"Re: An alliance of neighbours",
                f"Greetings {ctx['to']},\n\nI have accepted your invitation and joined {ctx['alliance']}. "
                f"My village is ready to stand with yours - may our merchants travel safely between us.\n\n{ctx['me']}")
    if kind == "invite_player":
        near = ctx.get("near")
        where = (f"{near['place']} lies {near['away']} from your {ctx['village']}"
                 if near else f"our villages are not far from your {ctx['village']}")
        return ((f"Still an open door: {ctx['alliance']}" if ctx.get("again") else f"Neighbours in arms: {ctx['alliance']}"),
                f"Greetings {ctx['to']},\n\nNews of a new settlement travels fast - {where}. "
                f"We of {ctx['alliance']} [{ctx['tag']}] look after our own: we send grain and timber to young villages, "
                f"and when raiders come we march to help, however few we can spare.\n\n"
                f"An invitation waits for you at your Embassy (you can build one from the Town Center at level 1). "
                f"Join us if you wish - and if you ever want to leave for another alliance, you part as a friend.\n\n{ctx['me']}")
    if kind == "welcome":
        near = ctx.get("near")
        help_line = (f"Your nearest friend is {near['place']}, {near['away']} away."
                     if near else "Our villages are spread across this half of the world.")
        return (f"Welcome to {ctx['alliance']}",
                f"Greetings {ctx['to']},\n\nWelcome to {ctx['alliance']} [{ctx['tag']}]! {help_line}\n\n"
                f"While your village is young we will send it resources now and then, and if you are attacked, "
                f"we will send what troops we can. Write to any of us whenever you need something - "
                f"we are not always at our desks, but we answer.\n\n{ctx['me']}")
    if kind == "gift":
        return (f"Supplies for {ctx['village']}",
                f"Greetings {ctx['to']},\n\nOur merchants are on the road to {ctx['village']} with {ctx['what']}. "
                f"A young village needs a hand - use it well, and grow strong.\n\n{ctx['me']}")
    if kind == "reinforce":
        return (f"Help is on the way to {ctx['village']}",
                f"Greetings {ctx['to']},\n\nOur scouts saw an enemy marching on {ctx['village']}. "
                f"I have sent {ctx['what']} to stand with you - not many, but you will not face them alone.\n\n{ctx['me']}")
    return ("Greetings", f"Greetings {ctx.get('to', '')},\n\n{ctx['me']}")


def _nearest(village, bot_villages):
    """The bot village nearest to `village` ({x, y}): {"bot", "village", "dist", "days"} or None."""
    best = None
    for v, owner in bot_villages:
        d = math.hypot(v["x"] - village["x"], v["y"] - village["y"])
        if best is None or d < best["dist"]:
            best = {"bot": owner, "village": v["name"], "x": v["x"], "y": v["y"], "dist": d}
    if best:
        # "AITeuton at Alesia (12|-4)"; a default name ("AITeuton's village") is not repeated
        name = best["village"]
        default = name.lower() in (f"{best['bot']}'s village".lower(), f"{best['bot']}s village".lower())
        best["place"] = f"{best['bot']} at ({best['x']}|{best['y']})" if default else f"{best['bot']} at {name} ({best['x']}|{best['y']})"
        n = max(1, round(best["dist"]))
        best["away"] = "one square" if n == 1 else f"about {n} squares"
    return best
