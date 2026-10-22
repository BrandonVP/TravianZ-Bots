"""Alliance building between the bots, through the normal Embassy and alliance pages.

Configured per bot in state/diplomacy.json (kept apart from plan.json, which the AI advisor rewrites):

    {"role": "founder", "tag": "ACCORD", "name": "The Accord",
     "invite": ["AIRoman", "AITeuton"]}                     # AIBot
    {"role": "member", "accept_from": ["The Accord", "ACCORD"],
     "founder": "AIBot"}                                     # the other bots

founder: with an Embassy at level 3 it founds the alliance, then invites each player on its list (again after a
         day if they still have not joined), sending each a personal message.
member:  with an Embassy at level 1 it accepts an invitation from an alliance on its accept_from list and thanks
         the founder. Until then it waits - the building targets in its plan get the Embassy built.

Runtime progress lives in state/diplomacy_state.json. The game decides everything; the bot only uses its pages.
"""
import json
import logging
import time

from . import parse, rules

log = logging.getLogger(__name__)

REINVITE_S = 24 * 3600


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
    return ("Greetings", f"Greetings {ctx.get('to', '')},\n\n{ctx['me']}")
