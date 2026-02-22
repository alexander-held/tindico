import requests

from .config import INDICO_API_TOKEN, INDICO_BASE_URL
from .models import Contribution, IndicoEvent, _parse_attachments, contribution_from_json, event_from_json


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
                contributions.append(contribution_from_json(entry, INDICO_BASE_URL))
            # Session blocks contain nested entries
            for nested in entry.get("entries", {}).values():
                if "startDate" in nested:
                    contributions.append(contribution_from_json(nested, INDICO_BASE_URL))
    contributions.sort(key=lambda c: c.start_dt)

    # The timetable endpoint often has files=null; enrich from event contributions endpoint
    contribs_without = [c for c in contributions if not c.attachments]
    if contribs_without:
        _enrich_attachments(event_id, contributions)

    return contributions


def _enrich_attachments(event_id: int, contributions: list[Contribution]) -> None:
    """Fetch attachment data from the event contributions endpoint and merge it in."""
    try:
        data = _get(
            f"/export/event/{event_id}.json",
            params={"detail": "contributions"},
        )
    except Exception:
        return
    # Build a lookup: title → list of attachments from the contributions endpoint
    att_by_title: dict[str, list[tuple[str, str]]] = {}
    for item in data.get("results", []):
        for c in item.get("contributions") or []:
            title = c.get("title", "")
            atts = _parse_attachments(c, INDICO_BASE_URL)
            if atts:
                att_by_title[title] = atts
    # Merge into contributions that lack attachments
    for c in contributions:
        if not c.attachments and c.title in att_by_title:
            c.attachments = att_by_title[c.title]


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


def get_category_info(category_id: int) -> dict:
    """Fetch category info (parent + subcategories) via /category/<id>/info.

    Returns {'id', 'title', 'parent_id', 'parent_name', 'subcategories': [{'id', 'title'}, ...]}
    """
    data = _get(f"/category/{category_id}/info")

    cat = data.get("category", {})

    # parent_path is ancestors (root→...→parent), not including self
    parent_id = None
    parent_name = ""
    parent_path = cat.get("parent_path", [])
    if parent_path:
        parent = parent_path[-1]
        parent_id = parent.get("id")
        parent_name = parent.get("title", "")

    subcategories = [
        {"id": sub["id"], "title": sub["title"]}
        for sub in data.get("subcategories", [])
    ]

    return {
        "id": cat.get("id", category_id),
        "title": cat.get("title", ""),
        "parent_id": parent_id,
        "parent_name": parent_name,
        "subcategories": subcategories,
    }
