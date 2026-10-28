"""HTTP session against the TravianZ web UI: login, page fetches, following action links."""
import hashlib
import logging
import time
from urllib.parse import urljoin

import requests

from . import parse

log = logging.getLogger(__name__)

WORLD_MAX = 200  # config.php WORLD_MAX; used for map tile ids


class GameError(Exception):
    pass


def tile_id(x, y, world_max=WORLD_MAX):
    """Map tile id (wref) for coordinates, as Generator::getBaseID()."""
    return (world_max - y) * (world_max * 2 + 1) + (world_max + x + 1)


def map_check(wref):
    """Checksum the map page wants next to a tile id, as Generator::getMapCheck()."""
    return hashlib.md5(str(wref).encode()).hexdigest()[5:7]


class GameClient:
    def __init__(self, base_url, user, password, request_gap=1.0, timeout=30):
        self.base = base_url.rstrip("/") + "/"
        self.user = user
        self.password = password
        self.request_gap = request_gap  # seconds between requests, to stay gentle
        self.timeout = timeout
        self.session = requests.Session()
        self.session.headers["User-Agent"] = "TravianZ-economy-bot/0.2"
        self._last_request = 0.0
        self.village_id = None  # currently selected village (None = game default)

    def _pace(self):
        wait = self._last_request + self.request_gap - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last_request = time.monotonic()

    def login(self):
        self._pace()
        r = self.session.post(urljoin(self.base, "login.php"), timeout=self.timeout, data={
            "ft": "a4", "user": self.user, "pw": self.password, "s1": "login"})
        r.raise_for_status()
        if parse.is_login_page(r.text):
            raise GameError("login failed (wrong credentials, banned or vacation mode?)")
        log.info("logged in as %s", self.user)
        if self.village_id is not None:
            self._request("GET", f"dorf1.php?newdid={self.village_id}")

    def _request(self, method, path, data=None):
        self._pace()
        r = self.session.request(method, urljoin(self.base, path), data=data, timeout=self.timeout)
        r.raise_for_status()
        return r.text

    def _call(self, method, path, data=None):
        """Request a page, logging in again (and reselecting the village) if the session expired."""
        for attempt in (1, 2):
            text = self._request(method, path, data)
            if not parse.is_login_page(text):
                return text
            if attempt == 1:
                log.info("session expired, logging in again")
                self.login()
        raise GameError(f"still on login page after re-login: {path}")

    def get(self, path):
        return self._call("GET", path)

    def post(self, path, data):
        return self._call("POST", path, data)

    # --- villages -------------------------------------------------------------

    def select_village(self, village_id):
        """Make village_id the current village for the following requests."""
        self.village_id = village_id
        self.get(f"dorf1.php?newdid={village_id}")

    def village(self):
        """Current village state: resources, fields, buildings, queue, troops, incoming attacks."""
        d1 = parse.parse_dorf1(self.get("dorf1.php"))
        d2 = parse.parse_dorf2(self.get("dorf2.php"))
        return {"resources": d1["resources"], "fields": d1["fields"],
                "buildings": d2["buildings"], "queue": d1["queue"] or d2["queue"],
                "troops": d1["troops"], "incoming": d1["incoming"], "villages": d1["villages"],
                "gold": d1["gold"]}

    def capital_id(self):
        """Village id of the capital (from the own profile), or None."""
        uid = parse.parse_own_uid(self.get("dorf1.php"))
        return parse.parse_capital(self.get(f"spieler.php?uid={uid}")) if uid else None

    # --- gold -----------------------------------------------------------------

    def finish_job(self, href):
        """Finish one running job with village gold (the queue's "Finish now" link, e.g. '?finishJob=..&c=..')."""
        self.get(("dorf1.php" + href) if href.startswith("?") else href)

    # --- messages -------------------------------------------------------------

    def send_message(self, to, subject, text):
        """Send an in-game message to player `to`."""
        self.post("nachrichten.php", {"ft": "m2", "c": "3e9", "an": to, "be": subject[:60], "message": text, "s1": "ok"})

    # --- marketplace ----------------------------------------------------------

    def market(self, slot):
        """Marketplace send page: {"merchants", "total", "carry", "sending_to"} or None."""
        return parse.parse_market(self.get(f"build.php?id={slot}"))

    def send_resources(self, slot, x, y, amounts):
        """Ship {"wood","clay","iron","crop"} to (x|y) from the Marketplace at `slot`.

        Two steps like the page: the game first shows a confirmation (only when merchants, resources and
        the target are fine), then the confirmed form sends. Returns True if the confirmation came back.
        """
        data = {"ft": "check", "id": slot, "send3": 1, "x": x, "y": y, "dname": "", "s1": "ok"}
        for i, k in enumerate(("wood", "clay", "iron", "crop"), start=1):
            data[f"r{i}"] = int(amounts.get(k, 0))
        confirm = parse.parse_send_confirm(self.post("build.php", data))
        if confirm is None:
            return False
        self.post("build.php", confirm)
        return True

    def send_reinforcement(self, x, y, unit_index, count, unit_id, extra=None):
        """Send `count` troops of the tribe's unit number `unit_index` (1-10) from the current village to
        reinforce (x|y), via the Rally Point form (c=2) and its confirmation. True if the troops left.
        `extra` adds more form slots to the same send, e.g. {13: 5} for Archery Range units."""
        data = {"b": 1, "c": 2, "x": x, "y": y, "dname": "", "disabledr": "", "disabled": "",
                f"t{unit_index}": int(count), "s1": "ok"}
        for idx, n in (extra or {}).items():
            data[f"t{idx}"] = int(n)
        confirm = parse.parse_a2b_confirm(self.post("a2b.php", data))
        if confirm is None:
            return False
        before = parse.parse_troops(parse._soup(self.get("dorf1.php"))).get(unit_id, 0)
        self.post("a2b.php", confirm)
        after = parse.parse_troops(parse._soup(self.get("dorf1.php"))).get(unit_id, 0)
        return after < before

    def send_troops(self, x, y, c, slots, check_unit=None):
        """Send troops from the current village to (x|y) with the Rally Point form: c=4 raid, c=3 attack
        (only scouts -> the game makes it a scouting mission, resources and troops), c=2 reinforcement.
        `slots` maps form slot (1-10 tribe units, 11+ extra units) -> count. True if the game accepted it
        (and, with `check_unit`, if that many fewer of it are at home afterwards)."""
        data = {"b": 1, "c": c, "x": x, "y": y, "dname": "", "disabledr": "", "disabled": "", "s1": "ok"}
        for slot, n in slots.items():
            data[f"t{slot}"] = int(n)
        page = self.post("a2b.php", data)
        confirm = parse.parse_a2b_confirm(page)
        if confirm is None or "beginners protection" in page:
            return False
        before = parse.parse_troops(parse._soup(self.get("dorf1.php"))).get(check_unit, 0) if check_unit else 0
        self.post("a2b.php", confirm)
        if not check_unit:
            return True
        after = parse.parse_troops(parse._soup(self.get("dorf1.php"))).get(check_unit, 0)
        return after < before

    def map_block(self, cx, cy):
        """The 17x17 map tiles around (cx|cy), as parse.parse_map_block()."""
        return parse.parse_map_block(self.get(f"ajax.php?f=k7&x={cx - 8}&y={cy - 8}&xx={cx + 8}&yy={cy + 8}"))

    def oasis_animals(self, x, y):
        """Animals in the unoccupied oasis at (x|y): a number (0 = empty) or None if it isn't one."""
        wref = tile_id(x, y)
        return parse.parse_oasis_animals(self.get(f"karte.php?d={wref}&c={map_check(wref)}"))

    def reports(self):
        """The newest page of reports: [{"id", "topic"}]."""
        return parse.parse_report_list(self.get("berichte.php"))

    def report(self, rid):
        """One report (opening it marks it read), as parse.parse_report()."""
        return parse.parse_report(self.get(f"berichte.php?id={int(rid)}"))

    def coordinates(self):
        """(x, y) of the current village (the map opens centred on it)."""
        return parse.parse_map_center(self.get("karte.php"))

    # --- archers (Empires of Rome Archery Range) -------------------------------

    def archers_enabled(self):
        """True if this server has the Archery Range units (the manual lists the building only then)."""
        return "gid=52" in self.get("manual.php?typ=3&s=2")

    def research_link(self, academy_slot, unit_id):
        """Academy research link for `unit_id` if the game offers it right now, else None."""
        return parse.parse_research_link(self.get(f"build.php?id={academy_slot}"), unit_id)

    # --- building -------------------------------------------------------------

    def inspect_upgrade(self, slot):
        """Look at an existing field/building: {"href", "cost", "duration", "status"}."""
        return parse.parse_build_page(self.get(f"build.php?id={slot}"))

    def inspect_construct(self, slot, name):
        """Look at an empty site: {"href", "cost"} for one building, or None if not listed."""
        return parse.parse_construct_page(self.get(f"build.php?id={slot}")).get(name)

    def follow(self, href):
        """Follow an action link (upgrade/construct) obtained from an inspect_* call."""
        self.get(href)

    # --- troops ---------------------------------------------------------------

    def train_page(self, slot):
        return parse.parse_train_page(self.get(f"build.php?id={slot}"))

    def train(self, slot, unit_id, count):
        """Order `count` units in the barracks/residence at `slot` (the game caps it at what we can afford)."""
        self.post("build.php", {"id": slot, "ft": "t1", f"t{unit_id}": int(count), "s1": "ok"})

    # --- expansion ------------------------------------------------------------

    def tile(self, x, y):
        wref = tile_id(x, y)
        info = parse.parse_tile(self.get(f"karte.php?d={wref}&c={map_check(wref)}"))
        info.update(x=x, y=y, wref=wref)
        return info

    def culture_points(self):
        """(have, needed for the next village) from the village page, or None."""
        return parse.parse_culture_points(self.get("dorf1.php"))

    def found_village(self, wref, settler=None):
        """Send 3 settlers from the current village to the free tile `wref`. True if the order was sent.

        The game answers a refused order (e.g. too few culture points) with a normal page, so the
        send is only counted when the settlers actually left the village.
        """
        if not parse.has_found_form(self.get(f"a2b.php?id={wref}&s=1")):
            return False
        before = parse.parse_troops(parse._soup(self.get("dorf1.php"))).get(settler, 0) if settler else None
        self.post("build.php", {"a": "new", "c": 5, "s": wref, "id": 39, "s1": "ok"})
        if settler:
            after = parse.parse_troops(parse._soup(self.get("dorf1.php"))).get(settler, 0)
            return after < before
        return True
