"""The champion (Champion's Hall): spend stat points, send it on quests, wear the best gear,
buy the Common set's missing pieces in the shop and bid on real upgrades in the Auction House.

The game prints a JSON copy of each Champion's Hall page for this module (ids tz-champ, tz-hero,
tz-quests, tz-auc), so nothing here depends on the page layout. All rules are in plan["champion"].
"""
import json
import logging
import re

log = logging.getLogger("travbot")

STAT_KEYS = {  # stat -> field of the points form (Templates/Build/37_hero.tpl, TZ_CH_STATS)
    "might": "off", "logistics": "deff", "command": "obonus", "fortitude": "dbonus",
    "vitality": "reg", "stewardship": "res", "insight": "ins",
}
# how much each stat point is worth to a champion of each role
ROLE_WEIGHTS = {
    "attack":  {"might": 3.0, "command": 3.0, "logistics": 1.5, "vitality": 1.0, "insight": 1.0, "fortitude": 0.5, "stewardship": 0.5},
    "defence": {"fortitude": 3.0, "vitality": 2.0, "might": 2.0, "stewardship": 1.0, "insight": 1.0, "logistics": 0.5, "command": 0.3},
    "economy": {"stewardship": 3.0, "insight": 2.0, "vitality": 1.5, "might": 1.0, "fortitude": 1.0, "logistics": 1.0, "command": 0.3},
}
AFFIX_WEIGHT = 0.1     # value of 1 % of an off-hand effect (speed, raid, vs Natars) in stat points
SET_WEIGHT = 2.0       # extra value of a set piece for every other piece of that set worn
GEAR_SLOTS = (1, 8, 9, 2, 10, 11, 5, 3, 4, 6)
_JSON = r'<script type="application/json" id="{}">(.*?)</script>'


def page_data(html, block):
    m = re.search(_JSON.format(block), html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))
    except ValueError:
        return None


def _checker(html):
    m = re.search(r'name="c" value="([^"]*)"', html)
    return m.group(1) if m else ""


class ChampionKeeper:
    def __init__(self, client, dry_run):
        self.client = client
        self.dry_run = dry_run

    # ------------------------------------------------------------------ scoring
    def score(self, item, weights, worn):
        """Value of a piece for this champion; worn = slot -> item currently worn (for set bonuses)."""
        if not item:
            return 0.0
        s = sum(weights.get(k, 0.5) * v for k, v in (item.get("stats") or {}).items())   # PHP sends [] when empty
        s += AFFIX_WEIGHT * sum(v for k, v in (item.get("bonus") or {}).items() if k != "horse_speed")
        s += 0.3 * (item.get("bonus") or {}).get("horse_speed", 0)
        if item.get("set"):
            mates = sum(1 for slot, w in worn.items()
                        if w and w.get("set") == item["set"] and int(slot) != item["slot"])
            s += SET_WEIGHT * mates
        return s

    @staticmethod
    def fits(item, family):
        return not item.get("family") or item["family"] == family

    # ------------------------------------------------------------------ one round
    def step(self, slot, cfg):
        """Run every champion rule once from the Champion's Hall in `slot` of the selected village."""
        actions = []
        weights = ROLE_WEIGHTS.get(cfg["role"], ROLE_WEIGHTS["economy"])
        base = f"build.php?id={slot}"

        inv_html = self.client.get(base + "&t4tab=items")
        inv = page_data(inv_html, "tz-champ")
        if not inv or not inv.get("hero"):
            return actions   # no champion (training one is left to the player)
        hero = inv["hero"]

        # 1. stat points, by the role's weights (never above 100 in a stat)
        if hero["points"] > 0:
            actions += self._spend_points(base, weights)

        # 2. the shop's Common set for empty slots (bought first, so it is worn before a quest)
        if cfg["shop"] and hero["home"]:
            for _ in range(3):
                bought = self._shop(base, inv, weights, cfg)
                if not bought:
                    break
                actions.append(bought)
                if self.dry_run:
                    break
                inv = page_data(self.client.get(base + "&t4tab=items"), "tz-champ") or inv

        # 3. wear the best gear (only possible at home)
        if hero["home"]:
            for _ in range(5):
                inv, done = self._equip_best(base, inv, weights)
                if not done:
                    break
                actions.append(done)

        # 4. spare gear: sell plain pieces to the merchant
        if cfg["sell_spare"]:
            actions += self._sell_spare(base, inv, weights, cfg)

        # 5. a quest
        if cfg["quests"] and hero["home"]:
            q = self._quest(base, hero, cfg)
            if q:
                actions.append(q)

        # 6. the Auction House
        if cfg["auction"]:
            b = self._bid(base, inv, weights, cfg)
            if b:
                actions.append(b)
        return actions

    def _post(self, path, data, what):
        log.info("champion: %s", what)
        if self.dry_run:
            return None
        return self.client.post(path, data)

    def _spend_points(self, base, weights):
        html = self.client.get(base)
        h = page_data(html, "tz-hero")
        if not h or h["points"] <= 0:
            return []
        stats = dict(h["stats"])   # form key -> points
        top = sorted(weights, key=lambda k: -weights[k])
        add = {k: 0 for k in STAT_KEYS.values()}
        for _ in range(h["points"]):
            # the next point goes to the stat whose weight per point already spent is highest
            best = max((k for k in top if stats[STAT_KEYS[k]] + add[STAT_KEYS[k]] < 100),
                       key=lambda k: weights[k] / (1 + stats[STAT_KEYS[k]] + add[STAT_KEYS[k]]), default=None)
            if best is None:
                break
            add[STAT_KEYS[best]] += 1
        data = {"t4points": "1", **{f"p_{k}": str(v) for k, v in add.items()}}
        spent = {k: v for k, v in add.items() if v}
        self._post(base, data, f"spent {sum(spent.values())} points {spent}")
        return [{"action": "champion", "name": "stat points", "reason": json.dumps(spent)}]

    def _equip_best(self, base, inv, weights):
        family = inv["hero"]["family"]
        worn = {int(k): v for k, v in inv["equipped"].items()}
        best, gain = None, 0.5
        for item in inv["bag"]:
            if not self.fits(item, family):
                continue
            g = self.score(item, weights, worn) - self.score(worn.get(item["slot"]), weights, worn)
            if g > gain:
                best, gain = item, g
        if not best:
            return inv, None
        html = self._post(base + "&t4tab=items", {"t4action": "equip", "rowid": str(best["row"])},
                          f"equip item {best['item']} (+{gain:.1f})")
        new = page_data(html, "tz-champ") if html else None
        if not new:   # dry run: pretend it is worn so the loop moves on
            inv = {**inv, "equipped": {**inv["equipped"], str(best["slot"]): best},
                   "bag": [i for i in inv["bag"] if i["row"] != best["row"]]}
        else:
            inv = new
        return inv, {"action": "champion", "name": f"equip item {best['item']}", "reason": f"+{gain:.1f} value"}

    def _shop(self, base, inv, weights, cfg):
        if inv["village_gold"] < cfg["shop_min_gold"]:
            return None
        worn = {int(k): v for k, v in inv["equipped"].items()}
        have = {i["slot"] for i in inv["bag"] if self.fits(i, inv["hero"]["family"])}
        for item in inv["shop"]:
            if item["slot"] in worn or item["slot"] in have or not self.fits(item, inv["hero"]["family"]):
                continue
            if item["price"] > inv["village_gold"] - cfg["shop_min_gold"]:
                continue
            html = self.client.get(base + "&t4tab=shop")
            self._post(base + "&t4tab=shop", {"tzshop": "1", "itemid": str(item["item"]), "qty": "1", "c": _checker(html)},
                       f"bought item {item['item']} for {item['price']} gold")
            return {"action": "champion", "name": f"shop item {item['item']}", "reason": f"{item['price']} gold, empty slot"}
        return None

    def _sell_spare(self, base, inv, weights, cfg):
        """Sell gear that is no use: worse than what is worn (or another family's weapon), up to Rare."""
        out = []
        worn = {int(k): v for k, v in inv["equipped"].items()}
        for item in inv["bag"]:
            if item["rarity"] > cfg["sell_max_rarity"]:
                continue
            useless = not self.fits(item, inv["hero"]["family"]) or (
                item["slot"] in worn and self.score(item, weights, worn) <= self.score(worn[item["slot"]], weights, worn))
            if not useless:
                continue
            self._post(base + "&t4tab=items", {"t4action": "sellmerchant", "rowid": str(item["row"])},
                       f"sold spare item {item['item']} for {item['sell']} gold")
            out.append({"action": "champion", "name": f"sell item {item['item']}", "reason": f"spare, {item['sell']} gold"})
            if len(out) >= 3:
                break
        return out

    def _quest(self, base, hero, cfg):
        html = self.client.get(base + "&t4tab=adventures")
        q = page_data(html, "tz-quests")
        if not q or q["running"] or q["health"] < cfg["quest_min_health"]:
            return None
        health = q["health"]
        safe = [o for o in q["offers"]
                if o["hp"][1] <= health - cfg["quest_health_margin"]
                and (o["tier"] < 3 or health >= cfg["expedition_min_health"])]
        if not safe:
            return None
        # longer quests drop better gear; at equal tier the better gear chance wins
        o = max(safe, key=lambda o: (o["tier"], o["gear"]))
        self._post(base + "&t4tab=adventures", {"t4action": "startadv", "advid": str(o["id"])},
                   f"quest {o['id']} (tier {o['tier']}, {o['seconds'] // 60} min, health loss up to {o['hp'][1]})")
        return {"action": "champion", "name": "quest", "reason": f"tier {o['tier']}, {o['seconds'] // 60} min"}

    def _bid(self, base, inv, weights, cfg):
        html = self.client.get(base + "&t4tab=auction")
        a = page_data(html, "tz-auc")
        if not a or a["my_bids"] >= cfg["max_open_bids"]:
            return None
        worn = {int(k): v for k, v in inv["equipped"].items()}
        family = inv["hero"]["family"]
        cap_total = int(a["capital_gold"] * cfg["max_capital_share"])
        best = None
        for lot in a["lots"]:
            if lot["mine"] or lot["leading"] or not lot["slot"] or not self.fits(lot, family):
                continue
            gain = self.score(lot, weights, worn) - self.score(worn.get(lot["slot"]), weights, worn)
            if gain < cfg["min_gain"]:
                continue
            limit = min(cap_total, int(gain * cfg["gold_per_point"]))
            need = lot["price"] + (1 if lot["has_bid"] else 0)
            if need > limit:
                continue
            if not best or gain / max(1, need) > best[0]:
                best = (gain / max(1, need), lot, limit, gain)
        if not best:
            return None
        _, lot, limit, gain = best
        self._post(base + "&t4tab=auction", {"t4action": "bid", "aucid": str(lot["id"]), "maxbid": str(limit)},
                   f"bid on lot {lot['id']} (item {lot['item']}, +{gain:.1f}) up to {limit} gold, price now {lot['price']}")
        return {"action": "champion", "name": f"bid on item {lot['item']}", "reason": f"up to {limit} gold for +{gain:.1f}"}
