# TravianZ bots

Computer players for the [TravianZ redesign fork](https://github.com/BrandonVP/TravianZ) (branch `live`). Each bot plays
an ordinary account through the game's normal web pages - no database access, the game decides everything.
A rules engine does the playing; a local AI model (Ollama) rewrites the **plan** it follows when the PC is idle,
and writes the bots' in-game letters.

Running here: **AIBot** (Gauls), **AIRoman** (Romans) and **AITeuton** (Teutons), allied as *The Accord*.

Status page: **http://192.168.68.205:8090/** (refreshes every minute; also `/status.json`, `/plan.json`).

## Files

| Path | What |
|---|---|
| `travbot/parse.py` | HTML pages -> data (resources, fields, buildings, queue, troops, attacks, villages, training, map tiles) |
| `travbot/client.py` | login, re-login, village switching, upgrade/construct/train/found-village requests (1 s apart) |
| `travbot/rules.py` | pure decision logic: what to build, how many troops, save-up checks |
| `travbot/expand.py` | settlers, scanning for free valleys, founding villages |
| `travbot/advisor.py` | idle check + Ollama call that writes `state/plan.json` |
| `travbot/web.py` | status page |
| `travbot/main.py` | one thread per account, the loop over its villages |
| `travbot/diplomacy.py` | alliance: found / invite / accept at the Embassy, letters |
| `state*/bot-account.json` | bot login (keep private - all `state*` folders are git-ignored) |
| `state*/diplomacy.json` | alliance role (founder or member) |
| `state/plan.json` | current plan (from the AI or by hand); AI plans are ignored when older than 6 h, a plan with `"source": "manual ..."` stays in force until replaced |
| `state/status.json`, `advisor.json`, `expansion.json`, `bot.log` | runtime state |

## Each round, per village

1. **Under attack** (incoming attack on the overview page): train Phalanxes with up to
   `defense.emergency_share` of resources, and check again every 2 minutes.
2. **Save up:** inspect the top field and top building candidate; if one becomes affordable within
   `save_up_hours`, reserve it. Cheaper items may only go ahead if they delay it by at most 10 minutes.
3. **Expansion** (Residence 10, `expansion.enabled`, fewer than `max_villages`): scan 12 map tiles per round
   for free valleys, keep 3 settlers trained, then send them to the nearest valley with at least
   `min_crop_fields` croplands. The game checks culture points, resources and the Rally Point.
4. **Gold:** finish running build jobs with "Finish now" (50 village gold each), longest first, only jobs with at
   least `gold.min_minutes_left` left (5 min when the village's gold is nearly full), at most `gold.max_per_tick`.
   Every village keeps `gold.reserve`; the capital keeps `gold.capital_reserve` because it pays the weekly
   Plus/production-bonus auto-renewals (about 5,500 gold a week).
5. **Fields:** cropland first if net crop < `min_net_crop`, else the resource with the lowest weighted production.
6. **Buildings:** Warehouse/Granary when storage fills within `storage_buffer_hours`, then `building_targets` in order.
7. **Defence:** keep `defense.unit_target` Phalanxes, spending at most `defense.budget_share` per round.

After all villages: **resource sharing** (`share`). A village producing less than `share.young_ratio` of the
richest one is "young"; the richest villages with a Marketplace ship it resources (Marketplace send form) up to
`share.fill_to` of its storage, never below `share.donor_keep_share` of their own storage, skipping shipments under
`share.min_shipment` and donors whose merchants are already on the way to it.

The game has the final word: the bot only follows links and forms the game offers.

## Several bots

Every folder `state-*` next to `state/` that holds a `bot-account.json` is another account, run in its own thread
with its own plan, logs and status page (`/b/<name>`; `/` is the first bot). Now: `state` = AIBot (Gauls),
`state-roman` = AIRoman (Romans, (6|5)), `state-teuton` = AITeuton (Teutons, (-4|0)). `--only state-roman` runs one.

## Alliance (`travbot/diplomacy.py`)

Per bot in `state*/diplomacy.json` (not in plan.json, so the advisor cannot overwrite it):
AIBot is the **founder** of *The Accord* [ACCORD]: with an Embassy at level 3 it founds the alliance and invites
AIRoman and AITeuton (again after a day if they have not joined), writing each a letter. The others are
**members**: with an Embassy at level 1 they accept an invitation from The Accord and thank AIBot. Letters are
written by the local AI model when the advisor PC is idle, otherwise from templates. Progress:
`state*/diplomacy_state.json`.

## AI advisor

After a round, at most hourly (retry every 10 min while busy): ask `http://192.168.68.87:8911/idle.json`
(30 s without keyboard/mouse and GPU < 20%), skip if another Ollama model is loaded, then send a summary of
all villages to `gemma4:26b` (fallback `ornith:9b`) with a JSON schema. The validated reply becomes
`state/plan.json`; the model is unloaded right after answering. Any failure just keeps the current plan.

## Setup

1. Create a game account per bot and a folder for each: `state/` (the first bot) and `state-<name>/` for more,
   each with `bot-account.json`: `{"user": "...", "pw": "...", "tribe": 3}` (1 Romans, 2 Teutons, 3 Gauls).
2. Optionally a `plan.json` (else the built-in default plan) and a `diplomacy.json`.
3. Build the image (`Dockerfile`) and run it with this folder mounted at `/app`; settings are environment variables,
   see the top of `travbot/main.py`. Code changes only need a container restart.

## Testing

- One dry round of every bot (decides, places no orders): `python -m travbot.main --once --dry-run`
  (`--only state-roman` for one bot). Point `TRAVBOT_STATE_DIR` at a copy of the state folders when the live bots run.
