"""Bot mail with the local AI model: the bots answer players' letters and thank them for resources.

This takes over what was done by hand with mail/mailbot.py (same record file, mail/replied.json, so nothing
is answered twice). It runs inside each bot's own loop and uses that bot's logged-in GameClient, at most every
MAIL_EVERY_S. Like a person, a bot answers some minutes after a letter or a delivery arrives (REPLY_MIN_S..
REPLY_MAX_S), and only while it is "awake" (AWAKE_FROM..AWAKE_TO, server time).

The model only writes text; it cannot make the bot do anything. The facts it is given about what the bots
really do (gifts, defence) keep it from promising more. When the model is unreachable, nothing is sent and the
letter waits for the next round.

Enabled with TRAVBOT_MAIL_AI=1 (and TRAVBOT_OLLAMA_URL / TRAVBOT_ADVISOR_MODELS for the model).
"""
import json
import logging
import pathlib
import random
import re
import threading
import time

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

MAIL_EVERY_S = 10 * 60
REPLY_MIN_S = 5 * 60
REPLY_MAX_S = 40 * 60
AWAKE_FROM, AWAKE_TO = 7, 23          # server hours when the bots write
MAX_PER_ROUND = 3                     # letters written per bot per round
SYSTEM_SENDERS = {"", "multihunter", "support", "taskmaster", "nature", "natars", "system", "sentry"}
TRIBES = {1: "Romans", 2: "Teutons", 3: "Gauls", 6: "Egyptians", 7: "Huns", 8: "Spartans", 9: "Vikings"}
RES = ["wood", "clay", "iron", "crop"]
REPLY_SCHEMA = {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}

_lock = threading.Lock()   # replied.json is shared by all bot threads


class Mailer:
    def __init__(self, client, me, tribe, state_dir, ollama_url, models, all_bots):
        self.c = client
        self.me = me
        self.tribe = tribe
        self.state_dir = pathlib.Path(state_dir)
        self.ollama = ollama_url.rstrip("/")
        self.models = models
        self.bots = set(all_bots)
        self.record = self.state_dir.parent / "mail" / "replied.json"
        self.last = 0.0
        self.requests = None   # travbot.letters.Requests: letters from members become actions (set by main)

    # --- record (same format as mail/mailbot.py, plus "seen" first-sighting times) --------------

    def _load(self):
        try:
            return json.loads(self.record.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _update(self, fn):
        with _lock:
            st = self._load()
            fn(st)
            self.record.parent.mkdir(parents=True, exist_ok=True)
            tmp = self.record.with_suffix(".tmp")
            tmp.write_text(json.dumps(st, indent=2), encoding="utf-8")
            tmp.replace(self.record)

    # --- game pages ------------------------------------------------------------------------------

    def _inbox(self):
        out = []
        soup = BeautifulSoup(self.c.get("nachrichten.php"), "html.parser")
        for tr in soup.select("table#overview tbody tr"):
            a, s = tr.select_one("td.top a"), tr.select_one("td.send")
            if not a or not s or not s.find("a"):
                continue
            out.append({"id": int(re.search(r"id=(\d+)", a["href"]).group(1)), "subject": a.get_text(strip=True),
                        "sender": s.get_text(strip=True), "new": "(new)" in tr.get_text()})
        return out

    def _read(self, mid):
        soup = BeautifulSoup(self.c.get(f"nachrichten.php?id={mid}"), "html.parser")
        msg = soup.select_one("#message") or soup.select_one("div.message")
        return msg.get_text("\n", strip=True)[:2000] if msg else ""

    def _deliveries(self, pages=2):
        out = []
        for p in range(pages):
            soup = BeautifulSoup(self.c.get(f"berichte.php?t=2&s={p * 20}&o=0"), "html.parser")
            rows = soup.select("table#overview tbody tr")
            for tr in rows:
                a = tr.select_one("td.sub a")
                if a and "send resources to" in a.get_text():
                    out.append((int(re.search(r"id=(\d+)", a["href"]).group(1)), a.get_text(strip=True)))
            if len(rows) < 20:
                break
        return out

    def _trade(self, rid):
        soup = BeautifulSoup(self.c.get(f"berichte.php?id={rid}"), "html.parser")
        t = soup.select_one("table#trade")
        if not t:
            return None
        pl = t.select_one('thead a[href*="spieler.php?uid="]')
        txt = t.get_text(" ")
        nums = [int(n) for n in re.findall(r"\d+", txt[txt.find("Resources"):])][:4]
        return {"sender": pl.get_text(strip=True) if pl else "", "amounts": dict(zip(RES, nums))}

    # --- the model -------------------------------------------------------------------------------

    def _alliance(self):
        try:
            d = json.loads((self.state_dir / "diplomacy.json").read_text(encoding="utf-8"))
            return f'"{d.get("name")}" [{d.get("tag")}]' if d.get("name") else None
        except (OSError, ValueError):
            return None

    def _facts(self):
        """What the bot really has, from the status.json its own loop writes every round."""
        try:
            st = json.loads((self.state_dir / "status.json").read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return ""
        lines, needs = [], []
        for v in st.get("villages") or []:
            res = v.get("resources") or {}
            x, y = (v.get("coords") or [None, None])[:2]
            parts, fills = [], {}
            for k in ("wood", "clay", "iron", "crop"):
                r = res.get(k) or {}
                amt, cap, prod = int(r.get("amount", 0)), int(r.get("cap", 0) or 1), int(r.get("prod", 0))
                fills[k] = amt / cap
                parts.append(f"{k} {amt:,}/{cap:,} ({prod:+,}/h)")
            size = sum(int(f.get("level", 0)) for f in (v.get("fields") or []))
            build = (v.get("queue") or [{}])[0].get("name")
            lines.append(f"- {v.get('name')} ({x}|{y}), fields total level {size}: " + ", ".join(parts)
                         + (f"; building {build}" if build else ""))
            low = min(fills, key=fills.get) if fills else None
            needs.append((size, sum(fills.values()) / max(1, len(fills)), v.get("name"), x, y, low))  # youngest first
        if not lines:
            return ""
        needs.sort()
        size, avg, name, x, y, low = needs[0]
        nl = chr(10)
        return ("Your villages right now (resources: amount/storage, production per hour):" + nl + nl.join(lines)
                + nl + f"The village that would benefit most from help is your youngest: {name} ({x}|{y}), lowest on {low}. "
                  "If a player offers resources, that is where they should send them." + nl)

    def _write(self, task, done=None):
        ally = self._alliance()
        who = (f"You are {self.me}, a ruler of the {TRIBES.get(self.tribe, 'a tribe')} in Empires of Rome, a Roman-era "
               f"strategy game" + (f", and a leader in the alliance {ally}" if ally else "") + ". ")
        facts = ("Facts you must not go beyond: you help members of your alliance - gifts of resources to young "
                 "villages and on request, troops to defend members' villages near yours, and you strike back at players "
                 "who attack your alliance. You never promise anything that is not listed under 'What you did', and "
                 "never gold. If asked for something else, answer kindly and vaguely. Never mention being a bot, AI or "
                 "a program. ")
        if done:
            facts += ("What you did about this letter (tell them plainly and exactly, add nothing): "
                      + " ".join(done) + " ")
        style = (f"Answer the player's actual question first, directly and concretely, using ONLY the facts above: "
                 f"if they ask which village or what resource, name it exactly with its coordinates. Never invent "
                 f"village names, places, numbers or people. If the facts don't answer the question, say so honestly. "
                 f"Then stay in character: friendly, plain English, under 500 characters, no lists. Sign exactly as {self.me}.")
        for model in self.models:
            try:
                r = requests.post(f"{self.ollama}/api/chat", timeout=420, json={
                    "model": model, "stream": False, "think": False, "format": REPLY_SCHEMA, "keep_alive": "10m",
                    "options": {"temperature": 0.7, "num_ctx": 2048, "num_predict": 600},
                    "messages": [{"role": "system", "content": who + facts + self._facts() + style},
                                 {"role": "user", "content": task}]})
                r.raise_for_status()
                text = str(json.loads(r.json()["message"]["content"])["text"]).strip().strip('{}"').strip()
                if 20 <= len(text) <= 1500:
                    return text
            except (requests.RequestException, ValueError, KeyError) as e:
                log.warning("mail by %s failed: %s", model, e)
        return None

    # --- one round -------------------------------------------------------------------------------

    def tick(self):
        now = time.time()
        if now - self.last < MAIL_EVERY_S:
            return
        self.last = now
        if not AWAKE_FROM <= time.localtime(now).tm_hour < AWAKE_TO:
            return
        try:
            self._baseline()
            self._letters(now)
            self._thanks(now)
        except Exception:  # mail must never stop the bot
            log.exception("mail round failed")

    def _baseline(self):
        """First round for this bot: letters already read and deliveries already there were handled by hand
        (mail/mailbot.py) - don't answer them again, late."""
        if self.me in self._load().get("baseline", {}):
            return
        old = [m["id"] for m in self._inbox() if not m["new"]]
        reports = [rid for rid, _ in self._deliveries()]

        def fn(st):
            st.setdefault(self.me, [])
            st[self.me] = sorted(set(st[self.me]) | set(old))
            th = st.setdefault("thanked", {}).setdefault(self.me, [])
            st["thanked"][self.me] = sorted(set(th) | set(reports))
            st.setdefault("baseline", {})[self.me] = int(time.time())
        self._update(fn)
        log.info("mail: baseline - %d read letter(s), %d delivery report(s) count as handled", len(old), len(reports))

    def _due(self, key, now):
        """First sighting starts a human-like delay; True once it has passed."""
        holder = {}

        def fn(st):
            seen = st.setdefault("seen", {}).setdefault(self.me, {})
            if key not in seen:
                seen[key] = now + random.randint(REPLY_MIN_S, REPLY_MAX_S)
            holder["at"] = seen[key]
        self._update(fn)
        return now >= holder["at"]

    def _letters(self, now):
        st = self._load()
        done = set(st.get(self.me, []))
        sent = 0
        for m in self._inbox():
            if sent >= MAX_PER_ROUND:
                break
            if m["id"] in done or m["sender"] in self.bots or m["sender"].lower() in SYSTEM_SENDERS:
                continue
            if not self._due(f"m{m['id']}", now):
                continue
            body = self._read(m["id"])
            acts = None
            if self.requests is not None:
                acted = self._load().get("acted", {}).get(self.me, {})
                acts = acted.get(str(m["id"]))
                if acts is None:   # read the letter's requests and act on them - once, even if the reply fails
                    reqs = self.requests.read(m["sender"], m["subject"], body)
                    if reqs is None:
                        return   # model unavailable: try again next round
                    acts = self.requests.handle(m["sender"], reqs)
                    self._update(lambda s, d=acts: s.setdefault("acted", {}).setdefault(self.me, {}).__setitem__(
                        str(m["id"]), d))
            text = self._write(f'{m["sender"]} wrote you a letter with the subject "{m["subject"]}":\n---\n{body}\n---\n'
                               f"Write your reply to {m['sender']}.", acts)
            if not text:
                return   # model unavailable: try again next round
            subj = m["subject"] if m["subject"].lower().startswith("re:") else ("Re: " + m["subject"])[:35]
            self.c.send_message(m["sender"], subj, text)
            self._update(lambda s: s.setdefault(self.me, []).append(m["id"]))
            log.info("mail: answered %s (%s)", m["sender"], m["subject"])
            sent += 1

    def _thanks(self, now):
        st = self._load()
        skip = set(st.get("not_player", {}).get(self.me, [])) | set(st.get("thanked", {}).get(self.me, []))
        for rid, title in self._deliveries():
            if rid in skip:
                continue
            if re.match(r"AI\w*'s village", title):
                self._update(lambda s: s.setdefault("not_player", {}).setdefault(self.me, []).append(rid))
                continue
            if not self._due(f"r{rid}", now):
                continue
            r = self._trade(rid)
            if not r or not r["sender"] or r["sender"] in self.bots:
                self._update(lambda s: s.setdefault("not_player", {}).setdefault(self.me, []).append(rid))
                continue
            got = ", ".join(f"{n} {k}" for k, n in r["amounts"].items() if n)
            text = self._write(f"{r['sender']} just sent you resources as a gift ({got}). Write {r['sender']} a short "
                               f"thank-you letter.")
            if not text:
                return
            self.c.send_message(r["sender"], "Thanks for the resources", text)
            self._update(lambda s: s.setdefault("thanked", {}).setdefault(self.me, []).append(rid))
            log.info("mail: thanked %s for %s", r["sender"], got)
            return   # one thank-you per round is plenty
