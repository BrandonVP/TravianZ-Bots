"""Turn TravianZ HTML pages into plain Python data. No network access here."""
import re

from bs4 import BeautifulSoup

RESOURCES = ("wood", "clay", "iron", "crop")

# Resource-bar cell ids in res.tpl: l4=wood, l3=clay, l2=iron, l1=crop.
_RES_CELL = {"wood": "l4", "clay": "l3", "iron": "l2", "crop": "l1"}

FIELD_KIND = {"Woodcutter": "wood", "Clay Pit": "clay", "Iron Mine": "iron", "Cropland": "crop",
              "Gold Mine": "gold"}   # gold mines: village gold, fields 45/46 (or a converted field)
FIELD_SLOTS = set(range(1, 19)) | {45, 46}

# e.g. "Woodcutter Level 3" or "Woodcutter Level 3 (upgrade in progress)"
_LEVEL_TITLE = re.compile(r"^(.*?)\s+Level\s+(\d+)\b")
_SLOT_ID = re.compile(r"build\.php\?id=(\d+)")
_CONSTRUCT = re.compile(r"dorf2\.php\?a=(\d+)&(?:amp;)?id=(\d+)&(?:amp;)?c=\w+")
_UNIT_CLASS = re.compile(r"\bu(\d+)\b")
_NEWDID = re.compile(r"newdid=(\d+)")


def _soup(html):
    return BeautifulSoup(html, "html.parser")


def _ints(text):
    return [int(n) for n in re.findall(r"-?\d+", text)]


def duration_seconds(text):
    """'1:04:20' -> 3860. Returns None if it doesn't look like a duration."""
    parts = text.strip().split(":")
    if len(parts) != 3 or not all(p.strip().isdigit() for p in parts):
        return None
    h, m, s = (int(p) for p in parts)
    return h * 3600 + m * 60 + s


def is_login_page(html):
    return 'name="ft" value="a4"' in html


def parse_resources(soup):
    """Stock, capacity and hourly production from the resource bar (present on every page)."""
    out = {}
    for kind, cell_id in _RES_CELL.items():
        # old resource bar: <td id="l4">; redesigned cards: <span id="l4" class="tz-rraw">
        cell = soup.find(id=cell_id) or soup.select_one(".tz-r-%s .tz-rraw" % kind)
        if cell is None:
            raise ValueError(f"resource cell {cell_id} not found")
        amount, cap = cell.get_text(strip=True).split("/")
        # title holds production per hour; for crop it is already net of upkeep
        out[kind] = {"amount": int(amount), "cap": int(cap), "prod": int(float(cell.get("title", "0")))}
    return out


def parse_gold(soup):
    """Village gold from the gold card: {"amount", "cap", "prod"} or None when gold is off."""
    cell = soup.select_one(".tz-r-gold .tz-rraw")
    if cell is None:
        return None
    nums = re.findall(r"\d+(?:\.\d+)?", cell.get_text(strip=True))
    if len(nums) < 2:
        return None
    return {"amount": int(float(nums[0])), "cap": int(float(nums[1])), "prod": float(cell.get("title", "0") or 0)}


_FINISH_PRICE = re.compile(r"for\s+(\d+)\s+gold", re.I)


def parse_queue(soup):
    """Build queue from the 'building_contract' table (dorf1/dorf2).

    A job that is building now and can be paid for has a "Finish now" link (finish_href, finish_price).
    """
    table = soup.find("table", id="building_contract")
    if table is None:
        return []
    queue = []
    for row in table.find_all("tr"):
        link = row.find("a", href=_SLOT_ID)
        timer = row.find("span", id=re.compile(r"^timer\d+$"))
        if link is None or timer is None:
            continue
        # old queue: "(Level 12)"; redesigned queue: "-> Level 12"
        level = re.search(r"Level\s+(\d+)", row.get_text(" ", strip=True))
        finish = row.find("a", class_="tz-job-finish")
        price = _FINISH_PRICE.search(finish.get("onclick", "") or finish.get("title", "")) if finish else None
        queue.append({
            "slot": int(_SLOT_ID.search(link["href"]).group(1)),
            "name": link.get_text(strip=True),
            "to_level": int(level.group(1)) if level else None,
            "seconds_left": duration_seconds(timer.get_text()) or 0,
            "finish_href": finish["href"].replace("&amp;", "&") if finish else None,
            "finish_price": int(price.group(1)) if price else None,
        })
    return queue


def parse_troops(soup):
    """Troops currently in the village (own + reinforcements), as {unit_id: count}."""
    table = soup.find("table", id="troops")
    troops = {}
    if table is None:
        return troops
    for row in table.find_all("tr"):
        img = row.find("img", class_="unit")
        num = row.find("td", class_="num")
        if img is None or num is None:
            continue
        m = _UNIT_CLASS.search(" ".join(img.get("class", [])))
        if m:
            troops[int(m.group(1))] = int(num.get_text(strip=True) or 0)
    return troops


def parse_incoming(soup):
    """Incoming attacks from the movements table: {"attacks": n, "first_in": seconds or None}."""
    out = {"attacks": 0, "first_in": None}
    table = soup.find("table", id="movements")
    if table is None:
        return out
    for row in table.find_all("tr"):
        img = row.find("img", class_="att1")
        if img is None:
            continue
        nums = _ints(row.find("div", class_="mov").get_text(" ", strip=True)) if row.find("div", class_="mov") else []
        timer = row.find("span", id=re.compile(r"^timer\d+$"))
        out["attacks"] += nums[0] if nums else 1
        secs = duration_seconds(timer.get_text()) if timer else None
        if secs is not None and (out["first_in"] is None or secs < out["first_in"]):
            out["first_in"] = secs
    return out


def parse_villages(soup):
    """Village list (only shown when the account has more than one village)."""
    table = soup.find("table", id="vlist")
    villages = []
    if table is None:
        return villages
    for row in table.find_all("tr"):
        link = row.find("a", href=_NEWDID)
        if link is None:
            continue
        coords = _ints(row.find("td", class_="aligned_coords").get_text(" ", strip=True)) \
            if row.find("td", class_="aligned_coords") else []
        villages.append({
            "id": int(_NEWDID.search(link["href"]).group(1)),
            "name": link.get_text(strip=True),
            "x": coords[0] if len(coords) > 1 else None,
            "y": coords[1] if len(coords) > 1 else None,
            "current": "hl" in (row.find("td", class_="dot") or {}).get("class", []),
        })
    return villages


def _areas(soup):
    """Map slot id -> area title for the village/field maps (deduplicated)."""
    areas = {}
    for area in soup.find_all("area", href=_SLOT_ID):
        slot = int(_SLOT_ID.search(area["href"]).group(1))
        areas.setdefault(slot, area.get("title", "").strip())
    # redesigned field map: one <a class="fm-field" title="Woodcutter Level 3"> per field
    for link in soup.find_all("a", class_="fm-field", href=_SLOT_ID):
        slot = int(_SLOT_ID.search(link["href"]).group(1))
        areas.setdefault(slot, link.get("title", "").strip())
    return areas


def parse_dorf1(html):
    soup = _soup(html)
    fields = []
    for slot, title in sorted(_areas(soup).items()):
        if slot not in FIELD_SLOTS:
            continue
        m = _LEVEL_TITLE.match(title)
        if not m or m.group(1) not in FIELD_KIND:
            continue
        fields.append({"slot": slot, "kind": FIELD_KIND[m.group(1)], "level": int(m.group(2))})
    return {"resources": parse_resources(soup), "fields": fields, "queue": parse_queue(soup),
            "troops": parse_troops(soup), "incoming": parse_incoming(soup),
            "villages": parse_villages(soup), "gold": parse_gold(soup)}


def parse_dorf2(html):
    """Village centre: slot -> {"name", "level"} for buildings, None for empty sites."""
    soup = _soup(html)
    buildings = {}
    for slot, title in _areas(soup).items():
        if not 19 <= slot <= 44:   # 41-44: the four extra building slots
            continue
        m = _LEVEL_TITLE.match(title)
        buildings[slot] = {"name": m.group(1), "level": int(m.group(2))} if m else None
    return {"buildings": buildings, "queue": parse_queue(soup)}


def parse_build_page(html):
    """build.php?id=N for an existing field/building: upgrade link (if allowed now) and cost."""
    soup = _soup(html)
    contract = soup.find("p", id="contract")
    result = {"href": None, "cost": None, "duration": None, "status": None}
    if contract is None:
        return result
    nums = [int(s.get_text(strip=True)) for s in contract.find_all("span", class_="little_res")]
    if len(nums) == 4:
        result["cost"] = dict(zip(RESOURCES, nums))
    m = re.search(r"\d+:\d{2}:\d{2}", contract.get_text(" ", strip=True))
    if m:
        result["duration"] = duration_seconds(m.group(0))
    link = contract.find("a", class_="build")
    if link is not None:
        result["href"] = link["href"].replace("&amp;", "&")
    status = contract.find("span", class_="none")
    if status is not None:
        result["status"] = status.get_text(" ", strip=True)
    return result


def parse_construct_page(html):
    """build.php?id=N for an empty site: building name -> {"href" or None, "cost" or None}.

    href is only present when the game would let us build it right now.
    """
    soup = _soup(html)
    options = {}
    for h2 in soup.find_all("h2"):
        name = h2.get_text(strip=True)
        entry = {"href": None, "cost": None}
        for el in h2.find_all_next():
            if el.name == "h2":
                break
            if el.name == "td" and "res" in el.get("class", []) and entry["cost"] is None:
                parts = [p for p in el.get_text(" ", strip=True).split("|")]
                nums = [_ints(p) for p in parts[:4]]
                if len(nums) == 4 and all(n for n in nums):
                    entry["cost"] = dict(zip(RESOURCES, (n[0] for n in nums)))
            href = el.get("href") if el.name == "a" else None
            if href and _CONSTRUCT.search(href) and entry["href"] is None:
                entry["href"] = href.replace("&amp;", "&")
        options[name] = entry
    return options


def parse_train_page(html):
    """Barracks/Residence page: trainable units and what is in training.

    Returns {"form": bool, "units": {uid: {"cost", "max", "available"}}, "training": {uid: count}}.
    """
    soup = _soup(html)
    form = soup.find("form", attrs={"name": "snd"})
    out = {"form": form is not None, "units": {}, "training": {}}
    if form is not None:
        for row in form.find_all("tr"):
            inp = row.find("input", attrs={"name": re.compile(r"^t\d+$")})
            img = row.find("img", class_="unit")
            if inp is None or img is None:
                continue
            uid = int(inp["name"][1:])
            details = row.find("div", class_="details")
            nums = _ints(details.get_text(" ", strip=True).split(":")[0]) if details else []
            info = row.find("span", class_="info")
            maxcell = row.find("td", class_="max")
            out["units"][uid] = {
                "cost": dict(zip(RESOURCES, nums[:4])) if len(nums) >= 4 else None,
                "available": (_ints(info.get_text()) or [0])[0] if info else 0,
                "max": (_ints(maxcell.get_text()) or [0])[0] if maxcell else 0,
            }
    progress = soup.find("table", class_="under_progress")
    if progress is not None:
        for row in progress.find_all("tr"):
            img = row.find("img", class_="unit")
            desc = row.find("td", class_="desc")
            if img is None or desc is None:
                continue
            m = _UNIT_CLASS.search(" ".join(img.get("class", [])))
            n = _ints(desc.get_text(" ", strip=True))
            if m and n:
                uid = int(m.group(1))
                out["training"][uid] = out["training"].get(uid, 0) + n[0]
    return out


def parse_map_center(html):
    """(x, y) the map page is centred on (the current village when opened without params)."""
    soup = _soup(html)
    xp = soup.find("input", attrs={"name": "xp"})
    yp = soup.find("input", attrs={"name": "yp"})
    if xp is None or yp is None:
        return None
    return int(xp["value"]), int(yp["value"])


def parse_tile(html):
    """karte.php?d=..&c=.. tile view: {"free": bool, "fields": [wood, clay, iron, crop] or None}."""
    soup = _soup(html)
    dist = soup.find("table", id="distribution")
    if dist is None:
        return {"free": False, "fields": None}
    vals = [int(td.get_text(strip=True)) for td in dist.select("td.val")]
    return {"free": len(vals) == 4, "fields": vals if len(vals) == 4 else None}


def parse_culture_points(html):
    """(have, needed for the next village) from the culture points card, or None."""
    soup = _soup(html)
    box = soup.find(class_="tz-cp-num")
    if box is None:
        return None
    nums = _ints(box.get_text(" ", strip=True).replace(",", ""))
    return (nums[0], nums[1]) if len(nums) >= 2 else None


def parse_own_uid(html):
    """Own player id from the header's Profile link, or None."""
    for a in _soup(html).find_all("a", href=re.compile(r"spieler\.php\?uid=\d+")):
        if a.get_text(strip=True).lower() == "profile":
            return int(re.search(r"uid=(\d+)", a["href"]).group(1))
    return None


def parse_capital(html):
    """Village id (wref) of the capital from a profile page's village list, or None."""
    table = _soup(html).find("table", id="villages")
    if table is None:
        return None
    for td in table.find_all("td", class_="nam"):
        if "(Capital)" in td.get_text(" ", strip=True):
            link = td.find("a", href=re.compile(r"karte\.php\?d=\d+"))
            if link:
                return int(re.search(r"d=(\d+)", link["href"]).group(1))
    return None


def parse_market(html):
    """Marketplace send page: free/total merchants, carry per merchant, villages with merchants on the way.

    Returns {"merchants": n, "total": n, "carry": n, "sending_to": set(wref)} or None if there is no send form.
    """
    soup = _soup(html)
    if soup.find("input", attrs={"name": "ft", "value": "check"}) is None:
        return None
    mer = soup.find("td", class_="mer")
    nums = _ints(mer.get_text(" ", strip=True)) if mer else []
    carry = 0
    for p in soup.find_all("p"):
        if "carry" in p.get_text(" ", strip=True).lower() and p.find("b"):
            carry = (_ints(p.find("b").get_text()) or [0])[0]
            break
    sending = set()
    for h4 in soup.find_all("h4"):
        if "on the way" not in h4.get_text(" ", strip=True).lower():  # "Own merchants on the way"
            continue
        for el in h4.find_all_next():
            if el.name == "h4":
                break
            if el.name == "a" and re.search(r"karte\.php\?d=(\d+)", el.get("href", "")):
                sending.add(int(re.search(r"d=(\d+)", el["href"]).group(1)))
    return {"merchants": nums[0] if nums else 0, "total": nums[1] if len(nums) > 1 else 0,
            "carry": carry, "sending_to": sending}


def parse_send_confirm(html):
    """The confirm step of a resource shipment: form fields of the ft=mk1 form, or None if refused."""
    soup = _soup(html)
    for form in soup.find_all("form"):
        if form.find("input", attrs={"name": "ft", "value": "mk1"}) is None:
            continue
        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name or inp.get("type") == "image":
                continue
            data[name] = inp.get("value", "")
        data["s1"] = "ok"
        return data
    return None


def parse_a2b_confirm(html):
    """The confirm step of a troop order (a2b.php): form fields of the a=533374 form, or None if refused."""
    soup = _soup(html)
    for form in soup.find_all("form"):
        if form.find("input", attrs={"name": "a", "value": "533374"}) is None:
            continue
        data = {}
        for inp in form.find_all("input"):
            name = inp.get("name")
            if not name or inp.get("type") == "image":
                continue
            if inp.get("type") == "radio" and not inp.has_attr("checked"):
                continue
            data[name] = inp.get("value", "")
        data["s1"] = "ok"
        return data
    return None


def parse_embassy(html):
    """Embassy page: {"alliance": name or None, "invites": [{"alliance", "accept_href"}]}."""
    soup = _soup(html)
    out = {"alliance": None, "invites": []}
    info = soup.find("table", id="ally_info")
    if info is not None:
        rows = info.find_all("tr")
        for row in rows:
            th, td = row.find("th"), row.find("td")
            if th and td and th.get_text(strip=True).lower().startswith("name"):
                err = td.find("span", class_="error")
                if err:
                    err.extract()
                out["alliance"] = td.get_text(" ", strip=True)
        if out["alliance"] is None:
            out["alliance"] = "?"
        return out
    join = soup.find("table", id="join")
    if join is not None:
        for row in join.find_all("tr"):
            nam, acc = row.find("td", class_="nam"), row.find("td", class_="acc")
            if nam and acc and acc.find("a"):
                out["invites"].append({"alliance": nam.get_text(" ", strip=True),
                                       "accept_href": acc.find("a")["href"].replace("&amp;", "&")})
    return out


def parse_alliance_members(html):
    """Player names in the alliance overview's member table (empty if not in an alliance)."""
    table = _soup(html).find("table", id="member")
    if table is None:
        return []
    return [td.get_text(strip=True) for td in table.find_all("td", class_="pla")]


def parse_form_error(html):
    """The first non-empty error text on a page (e.g. an invitation that was refused), or None."""
    for el in _soup(html).select("p.error, span.error"):
        text = el.get_text(" ", strip=True)
        if text:
            return text
    return None


def has_found_form(html):
    """True if a2b.php?id=..&s=1 shows the 'found new village' form (settlers ready, tile free)."""
    soup = _soup(html)
    return soup.find("input", attrs={"name": "a", "value": "new"}) is not None
