import requests

from .config import INDICO_API_TOKEN, INDICO_BASE_URL
from .models import Contribution, IndicoEvent, contribution_from_json, event_from_json


def _get(endpoint: str, params: dict | None = None) -> dict:
    """Authenticated GET against the Indico HTTP Export API."""
    url = f"{INDICO_BASE_URL}{endpoint}"
    headers = {"Authorization": f"Bearer {INDICO_API_TOKEN}"}
    resp = requests.get(url, headers=headers, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json()


def get_favorite_events(limit: int = 100) -> list[IndicoEvent]:
    """Fetch upcoming events from the user's favorited categories."""
    data = _get(
        "/export/categ/favorites.json",
        params={"from": "today", "order": "start", "limit": str(limit)},
    )
    results = data.get("results", [])
    return [event_from_json(item) for item in results]


def get_timetable(event_id: int) -> list[Contribution]:
    """Fetch timetable entries for an event, returned as a flat list sorted by start time."""
    data = _get(f"/export/timetable/{event_id}.json")
    results = data.get("results", {})
    contributions: list[Contribution] = []
    # results is keyed by event_id → date → entry-id → entry dict
    event_data = results.get(str(event_id), {})
    for _date, entries in event_data.items():
        for _entry_id, entry in entries.items():
            # Top-level entries (breaks, contributions)
            if "startDate" in entry:
                contributions.append(contribution_from_json(entry))
            # Session blocks contain nested entries
            for nested in entry.get("entries", {}).values():
                if "startDate" in nested:
                    contributions.append(contribution_from_json(nested))
    contributions.sort(key=lambda c: c.start_dt)
    return contributions


def get_category_events(
    category_id: int,
    from_date: str = "-30d",
    to_date: str = "+30d",
    limit: int = 200,
) -> list[IndicoEvent]:
    """Fetch events in a category within a date range."""
    data = _get(
        f"/export/categ/{category_id}.json",
        params={
            "from": from_date,
            "to": to_date,
            "order": "start",
            "limit": str(limit),
        },
    )
    results = data.get("results", [])
    return [event_from_json(item) for item in results]


def get_event(event_id: int) -> IndicoEvent:
    """Fetch a single event by ID."""
    data = _get(f"/export/event/{event_id}.json")
    results = data.get("results", [])
    if not results:
        raise ValueError(f"Event {event_id} not found")
    return event_from_json(results[0])
