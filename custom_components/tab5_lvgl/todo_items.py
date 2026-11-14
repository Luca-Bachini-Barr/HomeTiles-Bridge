"""The retained `items` payload of a to-do list entity for the To-do tile.

Topic: ``{ha_prefix}/todo/<object>/items`` (retained). The panel shows the
open items of one Home Assistant to-do list and may tick one off:

  {"v":1,"state":"2","f":15,"n":2,
   "i":[{"u":"a1b2","t":"Buy milk"},{"u":"c3d4","t":"Call the plumber","d":"2026-11-20"}]}

"state" is the entity state (the number of open items, or "unavailable"),
"f" the entity's supported_features, "n" the number of open items Home
Assistant reported (it can exceed the items sent), "i" the open items in Home
Assistant's order: "u" the item's uid (left out when it is too long to send,
so the panel cannot tick that item off), "t" the summary and "d" the due day
(local, "YYYY-MM-DD", or "YYYY-MM-DDTHH:MM" when Home Assistant has a due
time) when there is one. The payload is bounded (items, text
bytes, total bytes); when it must drop items it drops the last ones.

Commands: ``{base}/cmnd/todo`` with ``{"entity_id": ..., "action":
"complete", "uid": ...}``. Only a uid the Bridge published for that list is
accepted, and only when the list supports updating items. Pure functions, no
Home Assistant imports: tests run them directly.
"""

from __future__ import annotations

import json
from datetime import date, datetime, tzinfo
from typing import Any, Dict, Iterable, List, Mapping, Optional, Tuple

TODO_DOMAIN = "todo"
PAYLOAD_VERSION = 1
# Title edits and new items without a state change (the count stays) only show
# up through the periodic refresh.
REFRESH_S = 5 * 60
# Coalesces state changes and snapshots of the same moment.
DEBOUNCE_S = 2.0
# How long one list's todo.get_items may take before the Bridge gives up
# and keeps its retained items.
FETCH_TIMEOUT_S = 20.0
MAX_ITEMS = 32
MAX_TITLE_BYTES = 63
MAX_UID_BYTES = 160
MAX_PAYLOAD_BYTES = 8192
MAX_COMMAND_BYTES = 512

# homeassistant.components.todo.TodoListEntityFeature.UPDATE_TODO_ITEM
FEATURE_UPDATE_ITEM = 4

STATUS_OPEN = "needs_action"
STATUS_DONE = "completed"


def _clip_utf8(text: Any, limit: int) -> str:
  value = " ".join(str(text or "").split())
  raw = value.encode("utf-8")
  if len(raw) <= limit:
    return value
  return raw[:limit].decode("utf-8", errors="ignore").rstrip()


def _due_day(value: Any, zone: Optional[tzinfo]) -> Optional[str]:
  """The local due of an item: "YYYY-MM-DD", or "YYYY-MM-DDTHH:MM" when it has
  a time; None without a readable one."""
  if isinstance(value, datetime):
    moment = value
  elif isinstance(value, date):
    return value.isoformat()
  elif isinstance(value, str):
    text = value.strip()
    if len(text) == 10:
      try:
        return date.fromisoformat(text).isoformat()
      except ValueError:
        return None
    try:
      moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
      return None
  else:
    return None
  if moment.tzinfo is not None and zone is not None:
    moment = moment.astimezone(zone)
  return f"{moment.date().isoformat()}T{moment.hour:02d}:{moment.minute:02d}"


def features_of(attributes: Mapping[str, Any]) -> int:
  try:
    value = int((attributes or {}).get("supported_features") or 0)
  except (TypeError, ValueError):
    return 0
  return value if value >= 0 else 0


def can_complete(attributes: Mapping[str, Any]) -> bool:
  return bool(features_of(attributes) & FEATURE_UPDATE_ITEM)


def normalise_items(items: Iterable[Any], zone: Optional[tzinfo]) -> List[Dict[str, Any]]:
  """The open items of a todo.get_items response as payload entries, in order."""
  out: List[Dict[str, Any]] = []
  for item in items or ():
    if not isinstance(item, dict):
      continue
    status = item.get("status")
    if status is not None and status != STATUS_OPEN:
      continue
    title = _clip_utf8(item.get("summary"), MAX_TITLE_BYTES)
    if not title:
      continue
    entry: Dict[str, Any] = {}
    uid = item.get("uid")
    if isinstance(uid, str) and uid and len(uid.encode("utf-8")) <= MAX_UID_BYTES:
      entry["u"] = uid
    entry["t"] = title
    due = _due_day(item.get("due"), zone)
    if due:
      entry["d"] = due
    out.append(entry)
  return out


def build_payload(items: Iterable[Any], state: Optional[str], features: int,
                  zone: Optional[tzinfo]) -> str:
  """The retained payload of a list whose open items are `items`."""
  entries = normalise_items(items, zone)
  total = len(entries)
  entries = entries[:MAX_ITEMS]

  def encode(chosen: List[Dict[str, Any]]) -> str:
    body = {
      "v": PAYLOAD_VERSION,
      "state": state if isinstance(state, str) and state else "unknown",
      "f": max(0, int(features or 0)),
      "n": total,
      "i": chosen,
    }
    return json.dumps(body, ensure_ascii=False, separators=(",", ":"))

  payload = encode(entries)
  while entries and len(payload.encode("utf-8")) > MAX_PAYLOAD_BYTES:
    entries.pop()
    payload = encode(entries)
  return payload


def unavailable_payload() -> str:
  """A list Home Assistant does not have (or reports unavailable)."""
  return build_payload([], "unavailable", 0, None)


def uids_of_payload(payload: str) -> Tuple[str, ...]:
  """The uids a published payload offers for ticking off."""
  try:
    body = json.loads(payload)
  except ValueError:
    return ()
  items = body.get("i") if isinstance(body, dict) else None
  if not isinstance(items, list):
    return ()
  return tuple(item["u"] for item in items if isinstance(item, dict) and isinstance(item.get("u"), str))


def items_of_response(response: Any, entity_id: str) -> Optional[List[Any]]:
  """The items list of a todo.get_items service response, None if missing."""
  if not isinstance(response, dict):
    return None
  result = response.get(entity_id)
  if not isinstance(result, dict):
    return None
  items = result.get("items")
  return items if isinstance(items, list) else None


def parse_todo_command(payload: Any) -> Optional[Dict[str, Any]]:
  """Decode a To-do command body; None for anything malformed."""
  if isinstance(payload, (bytes, bytearray)):
    try:
      payload = payload.decode("utf-8")
    except UnicodeDecodeError:
      return None
  if not isinstance(payload, str) or len(payload.encode("utf-8")) > MAX_COMMAND_BYTES:
    return None
  try:
    command = json.loads(payload)
  except ValueError:
    return None
  if not isinstance(command, dict) or not isinstance(command.get("action"), str):
    return None
  return command


def build_todo_service_call(command: Mapping[str, Any], state: Optional[str],
                            attributes: Mapping[str, Any],
                            published_uids: Iterable[str]) -> Optional[Tuple[str, Dict[str, Any]]]:
  """Service and data for a To-do command, or None when it is not allowed.

  The only action is "complete": it needs the update feature bit and a uid the
  Bridge published for the list, so a panel cannot touch other items.
  """
  if state in (None, "unavailable"):
    return None
  if command.get("action") != "complete" or not can_complete(attributes):
    return None
  uid = command.get("uid")
  if not isinstance(uid, str) or not uid or uid not in set(published_uids):
    return None
  return "update_item", {"item": uid, "status": STATUS_DONE}
