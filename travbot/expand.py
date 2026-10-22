"""Expansion: train settlers in a village with Residence 10 and send them to found a new village.

Steps per village (one small step per tick, so the bot never hammers the server):
1. keep 3 settlers home or in training (the game only allows training them at Residence 10+);
2. scan nearby map tiles a few per tick for free valleys, remembering results;
3. when 3 settlers are home, send them to the best valley found (needs Rally Point, 750 of each
   resource and enough culture points; the game decides, the bot just retries later).
"""
import json
import logging
import math
import time

from . import rules

log = logging.getLogger(__name__)

TILES_PER_TICK = 12
SETTLERS_NEEDED = 3
SEND_RETRY_S = 30 * 60


def _ring(cx, cy, radius):
    """Tiles at Chebyshev distance `radius` from (cx, cy), nearest (Euclidean) first."""
    tiles = [(cx + dx, cy + dy) for dx in range(-radius, radius + 1) for dy in range(-radius, radius + 1)
             if max(abs(dx), abs(dy)) == radius]
    return sorted(tiles, key=lambda t: math.hypot(t[0] - cx, t[1] - cy))


class Expansion:
    def __init__(self, state_dir):
        self.path = state_dir / "expansion.json"

    def _load(self):
        try:
            return json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {"villages": {}}

    def _save(self, data):
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=1), encoding="utf-8")
        tmp.replace(self.path)

    def step(self, client, vid, coords, state, plan, tribe, village_count, reserved):
        """Do at most one expansion step for the current village. Returns a log message or None.

        `reserved` is the save-up reservation (dict with "cost") or None; settler training respects it.
        """
        exp = plan["expansion"]
        if not exp["enabled"] or village_count >= exp["max_villages"]:
            return None
        if rules.building_level(state, "Residence") < 10 or coords is None:
            return None

        data = self._load()
        vs = data["villages"].setdefault(str(vid), {"radius": 0, "scanned": [], "valleys": [], "bad": []})
        settler = rules.settler_unit(tribe)
        home = state["troops"].get(settler, 0)
        cx, cy = coords
        msg = None

        # 1. scan for valleys (a few tiles per tick, while the settlers are being trained)
        if vs["radius"] < exp["search_radius"]:
            todo = [t for t in _ring(cx, cy, vs["radius"] + 1) if list(t) not in vs["scanned"]]
            for x, y in todo[:TILES_PER_TICK]:
                tile = client.tile(x, y)
                vs["scanned"].append([x, y])
                if tile["free"]:
                    vs["valleys"].append({"x": x, "y": y, "wref": tile["wref"], "fields": tile["fields"]})
            if len(todo) <= TILES_PER_TICK:
                vs["radius"] += 1  # this ring is done
            self._save(data)

        # 2. settlers
        if home < SETTLERS_NEEDED:
            slot = rules.find_slot(state, "Residence")
            page = client.train_page(slot)
            training = page["training"].get(settler, 0)
            unit = page["units"].get(settler)
            need = SETTLERS_NEEDED - home - training
            if need <= 0 or unit is None or unit["max"] < 1:
                return msg  # already training, or can't afford one yet (or no expansion slot)
            if reserved and rules.delays_reserved(reserved["cost"], state["resources"], unit["cost"]):
                return msg
            count = min(need, unit["max"])
            client.train(slot, settler, count)
            return f"train {count} settler(s) in Residence (slot {slot})"

        # 3. send settlers to the best known valley
        if time.time() - vs.get("last_send_attempt", 0) < SEND_RETRY_S:
            return None
        bad = set(vs["bad"])
        good = [v for v in vs["valleys"]
                if v["wref"] not in bad and v["fields"][3] >= exp["min_crop_fields"]]
        if not good:
            if vs["radius"] >= exp["search_radius"]:
                good = [v for v in vs["valleys"] if v["wref"] not in bad]  # settle for any valley
            if not good:
                return None
        target = min(good, key=lambda v: math.hypot(v["x"] - cx, v["y"] - cy))
        vs["last_send_attempt"] = time.time()
        # the game refuses the order without enough culture points: wait for them instead
        cp = client.culture_points()
        if cp and cp[0] < cp[1]:
            self._save(data)
            return f"waiting for culture points ({cp[0]}/{cp[1]}) before founding a village at ({target['x']}|{target['y']})"
        sent = client.found_village(target["wref"], settler)
        if not sent:
            # occupied meanwhile, or not enough culture points/resources yet
            tile = client.tile(target["x"], target["y"])
            if not tile["free"]:
                vs["bad"].append(target["wref"])
            self._save(data)
            return f"could not send settlers to ({target['x']}|{target['y']}) yet (culture points or resources?)"
        vs["sent"] = {"wref": target["wref"], "x": target["x"], "y": target["y"], "time": int(time.time())}
        self._save(data)
        return f"sent settlers to found a village at ({target['x']}|{target['y']}) fields {target['fields']}"

    def summary(self):
        data = self._load()
        return {vid: {"radius": v["radius"], "valleys_found": len(v["valleys"]), "sent": v.get("sent")}
                for vid, v in data["villages"].items()}
