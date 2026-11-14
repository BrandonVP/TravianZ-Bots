"""page_data(): a champion with nothing equipped arrives as "equipped": [] (PHP empty array) and must read as {}."""
from travbot.champion import page_data

def html(body):
    return '<script type="application/json" id="tz-champ">' + body + '</script>'

empty = page_data(html('{"equipped": [], "bag": []}'), "tz-champ")
assert empty["equipped"] == {}, empty
worn = page_data(html('{"equipped": {"3": {"item": 7}}, "bag": []}'), "tz-champ")
assert worn["equipped"] == {"3": {"item": 7}}, worn
assert {int(k): v for k, v in empty["equipped"].items()} == {}
assert page_data("<p>nothing</p>", "tz-champ") is None
print("equipped_empty_test: ok")
