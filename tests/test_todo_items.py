"""The To-do tile's retained `items` payload, its commands and the Bridge wiring."""

from __future__ import annotations

import asyncio
import json
import logging
import types
import unittest
from datetime import date, datetime, timedelta, timezone

from test_announcement_guard import extract
from test_view_navigation import ROOT, load_module

TODO = load_module("todo_items")
CONTROL = load_module("control_helpers")
SEARCH_SOURCE = (ROOT / "entity_search.py").read_text(encoding="utf-8")
LONDON_SUMMER = timezone(timedelta(hours=1))
ALL_FEATURES = 1 | 2 | 4 | 8


class PayloadTest(unittest.TestCase):
  def test_open_items_keep_their_order_uid_title_and_due_day(self):
    items = [
      {"uid": "a1", "summary": "Buy  milk", "status": "needs_action"},
      {"uid": "b2", "summary": "Done already", "status": "completed"},
      {"uid": "c3", "summary": "Call the plumber", "status": "needs_action", "due": "2026-11-20"},
      {"uid": "d4", "summary": "Late call", "status": "needs_action", "due": "2026-11-20T23:30:00+00:00"},
      {"uid": "e5", "summary": "   ", "status": "needs_action"},
      "not an item",
    ]
    payload = json.loads(TODO.build_payload(items, "3", ALL_FEATURES, LONDON_SUMMER))
    self.assertEqual((payload["v"], payload["state"], payload["f"], payload["n"]), (1, "3", ALL_FEATURES, 3))
    self.assertEqual(payload["i"], [
      {"u": "a1", "t": "Buy milk"},
      {"u": "c3", "t": "Call the plumber", "d": "2026-11-20"},
      # 23:30 UTC is 00:30 the next day in local time.
      {"u": "d4", "t": "Late call", "d": "2026-11-21T00:30"},
    ])

  def test_descriptions_travel_clipped_and_empty_ones_are_left_out(self):
    items = [{"uid": "a", "summary": "With", "description": "  Two   scoops\n of food "},
             {"uid": "b", "summary": "Without", "description": "   "},
             {"uid": "c", "summary": "Long", "description": "ü" * 150}]
    payload = json.loads(TODO.build_payload(items, "3", 4, None))
    self.assertEqual(payload["i"][0]["x"], "Two scoops of food")
    self.assertNotIn("x", payload["i"][1])
    self.assertLessEqual(len(payload["i"][2]["x"].encode("utf-8")), TODO.MAX_DESCRIPTION_BYTES)

  def test_a_long_uid_is_left_out_so_the_item_cannot_be_ticked(self):
    payload = json.loads(TODO.build_payload(
      [{"uid": "x" * (TODO.MAX_UID_BYTES + 1), "summary": "Shown", "status": "needs_action"}], "1", 4, None))
    self.assertEqual(payload["i"], [{"t": "Shown"}])
    self.assertEqual(TODO.uids_of_payload(json.dumps(payload)), ())

  def test_titles_are_clipped_by_utf8_bytes_without_breaking_characters(self):
    title = "ü" * 40
    payload = json.loads(TODO.build_payload([{"uid": "a", "summary": title}], "1", 4, None))
    clipped = payload["i"][0]["t"]
    self.assertLessEqual(len(clipped.encode("utf-8")), TODO.MAX_TITLE_BYTES)
    self.assertTrue(set(clipped) <= {"ü"})

  def test_item_and_byte_bounds_keep_the_true_count(self):
    many = [{"uid": f"u{i:03d}", "summary": f"Item {i}"} for i in range(TODO.MAX_ITEMS + 10)]
    payload = json.loads(TODO.build_payload(many, str(len(many)), 4, None))
    self.assertEqual(len(payload["i"]), TODO.MAX_ITEMS)
    self.assertEqual(payload["n"], TODO.MAX_ITEMS + 10)
    wide = [{"uid": "u" * 150 + str(i), "summary": "W" * 63} for i in range(TODO.MAX_ITEMS)]
    text = TODO.build_payload(wide, "32", 4, None)
    self.assertLessEqual(len(text.encode("utf-8")), TODO.MAX_PAYLOAD_BYTES)
    self.assertEqual(json.loads(text)["n"], TODO.MAX_ITEMS)

  def test_unavailable_and_service_response_shapes(self):
    self.assertEqual(json.loads(TODO.unavailable_payload()),
                     {"v": 1, "state": "unavailable", "f": 0, "n": 0, "i": []})
    self.assertEqual(TODO.items_of_response({"todo.home": {"items": [{"summary": "x"}]}}, "todo.home"),
                     [{"summary": "x"}])
    for response in (None, [], {}, {"todo.home": None}, {"todo.home": {"items": "x"}}):
      self.assertIsNone(TODO.items_of_response(response, "todo.home"))
    self.assertEqual(TODO.features_of({"supported_features": "15"}), 15)
    self.assertEqual(TODO.features_of({"supported_features": -1}), 0)
    self.assertEqual(TODO.features_of({}), 0)


class CommandTest(unittest.TestCase):
  def setUp(self) -> None:
    self.attrs = {"supported_features": ALL_FEATURES}

  def test_complete_needs_a_published_uid_and_the_update_feature(self):
    command = TODO.parse_todo_command(json.dumps({"entity_id": "todo.home", "action": "complete", "uid": "a1"}))
    self.assertEqual(TODO.build_todo_service_call(command, "2", self.attrs, ("a1", "c3")),
                     ("update_item", {"item": "a1", "status": "completed"}))
    self.assertIsNone(TODO.build_todo_service_call(command, "2", self.attrs, ("c3",)), "unknown uid")
    self.assertIsNone(TODO.build_todo_service_call(command, "2", {"supported_features": 1 | 2 | 8}, ("a1",)),
                      "list cannot update items")
    self.assertIsNone(TODO.build_todo_service_call(command, "unavailable", self.attrs, ("a1",)))
    self.assertIsNone(TODO.build_todo_service_call(command, None, self.attrs, ("a1",)))

  def test_only_the_complete_action_with_a_string_uid(self):
    for body in ({"action": "delete", "uid": "a1"}, {"action": "complete"}, {"action": "complete", "uid": 5},
                 {"action": "complete", "uid": ""}):
      self.assertIsNone(TODO.build_todo_service_call(body, "2", self.attrs, ("a1", "5")), body)

  def test_malformed_bodies(self):
    for payload in ("", "[]", "{", '{"action": 1}', None, b"\xff", "x" * (TODO.MAX_COMMAND_BYTES + 1)):
      self.assertIsNone(TODO.parse_todo_command(payload), payload)
    self.assertEqual(TODO.parse_todo_command(b'{"action":"complete"}'), {"action": "complete"})


class AnnounceTest(unittest.TestCase):
  PAYLOAD = json.dumps({"v": 1, "state": "2", "f": 15, "n": 2, "i": [
    {"u": "a1", "t": "Feed the cat", "d": "2026-11-14T22:30"},
    {"u": "b2", "t": "No time", "d": "2026-11-14"}]})

  def test_only_a_published_item_just_due_is_announced(self):
    cmd = {"action": "announce", "uid": "a1"}
    self.assertEqual(TODO.announce_title(cmd, self.PAYLOAD, datetime(2026, 11, 14, 22, 30)), "Feed the cat")
    self.assertEqual(TODO.announce_title(cmd, self.PAYLOAD, datetime(2026, 11, 14, 22, 44)), "Feed the cat")
    self.assertIsNone(TODO.announce_title(cmd, self.PAYLOAD, datetime(2026, 11, 14, 22, 29)), "not due yet")
    self.assertIsNone(TODO.announce_title(cmd, self.PAYLOAD, datetime(2026, 11, 14, 22, 45)), "too late")
    self.assertIsNone(TODO.announce_title({"action": "announce", "uid": "b2"}, self.PAYLOAD,
                                          datetime(2026, 11, 14, 22, 31)), "a due day without a time")
    self.assertIsNone(TODO.announce_title({"action": "announce", "uid": "zz"}, self.PAYLOAD,
                                          datetime(2026, 11, 14, 22, 31)), "unknown uid")
    self.assertIsNone(TODO.announce_title({"action": "complete", "uid": "a1"}, self.PAYLOAD,
                                          datetime(2026, 11, 14, 22, 31)))
    self.assertIsNone(TODO.announce_title(cmd, "", datetime(2026, 11, 14, 22, 31)))

  def test_reminder_follows_the_pipeline_language(self):
    self.assertEqual(TODO.reminder_message("Feed the cat", "en-GB"), "Reminder: Feed the cat")
    self.assertEqual(TODO.reminder_message("Katze", "de"), "Erinnerung: Katze")
    self.assertEqual(TODO.reminder_message("x", "xx"), "Reminder: x")
    self.assertEqual(TODO.reminder_message("x", None), "Reminder: x")

  def test_handler_plays_chime_then_reminder_once(self):
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    self.assertIn('if command.get("action") == "announce":', source)
    self.assertIn('"media_content_id": todo_items.ALARM_URL', source)
    self.assertIn('await self.hass.services.async_call("tts", "speak", data, blocking=True)', source)
    # Title, then the alarm, then the description, each after the last finished.
    body = source[source.index("  async def _async_announce_todo("):source.index("  def _stop_todo_updates(")]
    title = body.index('await speak(item["title"])')
    alarm = body.index('"media_content_id": todo_items.ALARM_URL')
    description = body.index('await speak(item["description"])')
    self.assertLess(title, alarm)
    self.assertLess(alarm, description)
    self.assertIn("StaticPathConfig(todo_items.ALARM_URL", source)
    self.assertTrue((ROOT / TODO.ALARM_FILE).is_file(), "the alarm sound ships with the Bridge")
    self.assertIn("if key in self._todo_announced:", source)

  def test_announce_item_carries_the_description(self):
    payload = json.dumps({"v": 1, "state": "1", "f": 15, "n": 1, "i": [
      {"u": "a1", "t": "Feed the cat", "d": "2026-11-14T22:30", "x": "Two scoops"}]})
    self.assertEqual(TODO.announce_item({"action": "announce", "uid": "a1"}, payload, datetime(2026, 11, 14, 22, 31)),
                     {"title": "Feed the cat", "description": "Two scoops"})
    self.assertEqual(TODO.announce_item({"action": "announce", "uid": "a1"}, self.PAYLOAD.replace("Feed", "Feed"),
                                        datetime(2026, 11, 14, 22, 31))["description"], "")


class ListWiringTest(unittest.TestCase):
  def test_todos_are_a_picker_list_panel_list_and_sealed_command(self):
    self.assertEqual(CONTROL.PANEL_LIST_DOMAINS["todos"], ("todo",))
    self.assertIn('"todos": ("todo",),', SEARCH_SOURCE)
    self.assertIn('"todos": ("todos",),', SEARCH_SOURCE)
    with self.assertRaises(ValueError):
      CONTROL.panel_entity_list(["todo.home", "script.x"], "todos")
    source = (ROOT / "__init__.py").read_text(encoding="utf-8")
    self.assertIn('"todo_meta": self._build_entity_meta(self.todos),', source)
    self.assertIn('"entity_search": 7,', source)
    self.assertIn("+ self.todos\n", source, "to-do state changes reach the Bridge")
    self.assertIn('"todo": self._async_handle_todo_command,', source)
    self.assertIn('f"{self.base_topic}/cmnd/todo"', source)
    channel = (ROOT / "command_channel.py").read_text(encoding="utf-8")
    self.assertIn('"todo"', channel)
    flow = (ROOT / "config_flow.py").read_text(encoding="utf-8")
    self.assertIn('todos = selected(CONF_TODOS, ("todo",))', flow)


METHODS = {
  "_schedule_todo_refresh", "_todo_interval_tick", "_async_process_todo_updates",
  "_async_publish_todo_items", "_stop_todo_updates", "_async_handle_todo_command",
  "_owns_state_publish", "_ha_topic_for_entity",
}


class PublisherWiringTest(unittest.IsolatedAsyncioTestCase):
  async def asyncSetUp(self) -> None:
    self.publishes: list[tuple[str, dict, bool]] = []
    self.hanging: set = set()
    self.calls: list[tuple] = []
    self.timers: list = []
    self.intervals: list = []
    self.response: object = {"todo.home": {"items": [
      {"uid": "a1", "summary": "Buy milk", "status": "needs_action"},
      {"uid": "c3", "summary": "Call the plumber", "status": "needs_action"}]}}
    self.states = {"todo.home": types.SimpleNamespace(state="2", attributes={"supported_features": ALL_FEATURES})}

    async def publish(hass, topic, payload, qos=0, retain=False):
      self.publishes.append((topic, json.loads(payload), retain))

    async def call(domain, service, data, blocking=False, return_response=False):
      self.calls.append((domain, service, dict(data), blocking, return_response))
      if data.get("entity_id") in self.hanging:
        await asyncio.Event().wait()
      if isinstance(self.response, Exception) and service == "get_items":
        raise self.response
      return self.response if return_response else None

    def call_later(hass, delay, action):
      self.timers.append((delay, action))
      return lambda: self.timers.remove((delay, action)) if (delay, action) in self.timers else None

    def interval(hass, action, period):
      self.intervals.append(period)
      return lambda: self.intervals.remove(period)

    scope: dict = {key: value for key, value in vars(TODO).items() if not key.startswith("__")}
    scope.update({
      "todo_items": TODO, "TODO_DOMAIN": TODO.TODO_DOMAIN, "TODO_REFRESH_S": TODO.REFRESH_S,
      "callback": lambda function: function, "entity_domain": CONTROL.entity_domain,
      "mqtt": types.SimpleNamespace(async_publish=publish), "async_call_later": call_later,
      "async_track_time_interval": interval, "DOMAIN": "tab5_lvgl", "timedelta": timedelta,
      "asyncio": asyncio,
      "dt_util": types.SimpleNamespace(now=lambda: datetime(2026, 11, 14, 9, 0, tzinfo=LONDON_SUMMER),
                                       get_default_time_zone=lambda: LONDON_SUMMER),
      "_LOGGER": logging.getLogger("test_todo_items"),
    })
    extract(METHODS, scope)
    bridge = type("Bridge", (), {name: scope[name] for name in METHODS})()
    bridge.hass = types.SimpleNamespace(
      data={"tab5_lvgl": {}}, states=types.SimpleNamespace(get=lambda e: self.states.get(e)),
      services=types.SimpleNamespace(async_call=call),
      async_create_task=lambda coro: asyncio.ensure_future(coro),
    )
    bridge.entry = types.SimpleNamespace(entry_id="entry1")
    bridge.ha_prefix = "ha"
    bridge.base_topic = "hometiles"
    bridge.todos = ["todo.home"]
    bridge._runtime_setup_complete = True
    bridge._todo_pending = set()
    bridge._todo_refresh_handle = None
    bridge._todo_task = None
    bridge._unsub_todo_interval = None
    bridge._todo_last_payload = {}
    bridge._secure_log_due = lambda key: False
    bridge._resolve_target_entity = lambda entity, allowed: entity if entity in allowed else None
    self.bridge = bridge

  async def run_due(self) -> None:
    for _delay, action in list(self.timers):
      self.timers.remove((_delay, action))
      action(None)
    for _ in range(5):
      await asyncio.sleep(0)

  async def test_refresh_is_coalesced_and_publishes_retained_items(self):
    self.bridge._schedule_todo_refresh(["todo.home", "todo.unknown"])
    self.bridge._schedule_todo_refresh(["todo.home"])
    self.assertEqual(len(self.timers), 1, "one debounce timer")
    self.assertEqual(self.intervals, [timedelta(seconds=TODO.REFRESH_S)], "periodic refresh started")
    await self.run_due()
    self.assertEqual(len(self.calls), 1)
    domain, service, data, blocking, response = self.calls[0]
    self.assertEqual((domain, service, blocking, response), ("todo", "get_items", True, True))
    self.assertEqual(data, {"entity_id": "todo.home", "status": ["needs_action"]})
    topic, payload, retain = self.publishes[0]
    self.assertEqual((topic, retain), ("ha/todo/home/items", True))
    self.assertEqual(payload["i"], [{"u": "a1", "t": "Buy milk"}, {"u": "c3", "t": "Call the plumber"}])
    # The same items again publish nothing.
    self.bridge._todo_interval_tick()
    await self.run_due()
    self.assertEqual(len(self.calls), 2)
    self.assertEqual(len(self.publishes), 1)

  async def test_failures_keep_the_retained_items_and_missing_lists_say_so(self):
    self.response = RuntimeError("list offline")
    self.bridge._schedule_todo_refresh(["todo.home"])
    await self.run_due()
    self.assertEqual(self.publishes, [])
    self.states.clear()
    self.bridge._schedule_todo_refresh(["todo.home"])
    await self.run_due()
    self.assertEqual(self.publishes[0][1]["state"], "unavailable")

  async def test_tick_off_only_a_published_item_then_refresh(self):
    self.bridge._schedule_todo_refresh(["todo.home"])
    await self.run_due()
    self.calls.clear()

    def message(body, retain=False):
      return types.SimpleNamespace(payload=json.dumps(body), retain=retain)

    await self.bridge._async_handle_todo_command(message({"entity_id": "todo.home", "action": "complete", "uid": "zz"}))
    await self.bridge._async_handle_todo_command(message({"entity_id": "todo.other", "action": "complete", "uid": "a1"}))
    await self.bridge._async_handle_todo_command(message({"entity_id": "todo.home", "action": "complete", "uid": "a1"},
                                                         retain=True))
    self.assertEqual(self.calls, [], "unknown uid, unknown list and retained commands do nothing")
    await self.bridge._async_handle_todo_command(message({"entity_id": "todo.home", "action": "complete", "uid": "a1"}))
    self.assertEqual(self.calls[0][:3], ("todo", "update_item",
                                         {"entity_id": "todo.home", "item": "a1", "status": "completed"}))
    self.assertEqual(len(self.timers), 1, "the shorter list is published right after")

  async def test_a_list_that_never_answers_does_not_block_the_others(self):
    TODO.FETCH_TIMEOUT_S = 0.05
    try:
      self.bridge.todos = ["todo.home", "todo.stuck"]
      self.states["todo.stuck"] = types.SimpleNamespace(state="1", attributes={"supported_features": ALL_FEATURES})
      self.hanging.add("todo.stuck")
      self.bridge._schedule_todo_refresh(["todo.stuck"])
      await self.run_due()
      await asyncio.sleep(0.1)
      self.assertTrue(self.bridge._todo_task.done(), "the stuck fetch gave up")
      self.bridge._schedule_todo_refresh(["todo.home"])
      await self.run_due()
      self.assertEqual([topic for topic, _, _ in self.publishes], ["ha/todo/home/items"])
    finally:
      TODO.FETCH_TIMEOUT_S = 20.0

  async def test_a_finished_task_does_not_block_later_refreshes(self):
    # Home Assistant may finish a task before async_create_task returns it
    # (eager start); the stored task is then done, never None.
    finished = asyncio.get_running_loop().create_future()
    finished.set_result(None)
    self.bridge._todo_task = finished
    self.bridge._schedule_todo_refresh(["todo.home"])
    await self.run_due()
    self.assertEqual([topic for topic, _, _ in self.publishes], ["ha/todo/home/items"])

  async def test_another_entry_owning_the_list_and_unload(self):
    self.bridge.hass.data["tab5_lvgl"]["state_owners"] = {("ha", "todo.home"): "entry2"}
    self.bridge._schedule_todo_refresh(["todo.home"])
    await self.run_due()
    self.assertEqual((self.calls, self.publishes), ([], []))
    self.bridge._schedule_todo_refresh(["todo.home"])
    self.bridge._stop_todo_updates()
    self.assertEqual((self.timers, self.intervals), ([], []))
    self.bridge._runtime_setup_complete = False
    self.bridge._schedule_todo_refresh(["todo.home"])
    self.assertEqual(self.timers, [])


if __name__ == "__main__":
  unittest.main()
