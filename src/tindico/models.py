from dataclasses import dataclass, field
from datetime import datetime
from zoneinfo import ZoneInfo


@dataclass
class IndicoEvent:
    id: int
    title: str
    url: str
    start_dt: datetime
    end_dt: datetime
    timezone: str
    description: str = ""
    location: str = ""
    category: str = ""
    category_id: int = 0
    event_type: str = ""


def parse_indico_datetime(dt_dict: dict) -> datetime:
    """Parse Indico's nested date format into a timezone-aware datetime.

    Expected format: {"date": "2025-03-15", "time": "14:00:00", "tz": "Europe/Zurich"}
    """
    tz = ZoneInfo(dt_dict["tz"])
    naive = datetime.strptime(f"{dt_dict['date']} {dt_dict['time']}", "%Y-%m-%d %H:%M:%S")
    return naive.replace(tzinfo=tz)


@dataclass
class Contribution:
    title: str
    start_dt: datetime
    end_dt: datetime
    speakers: list[str]
    attachments: list[tuple[str, str]] = field(default_factory=list)


def _parse_attachments(entry: dict, base_url: str) -> list[tuple[str, str]]:
    """Extract (title, url) pairs from both attachments.files and folders[].attachments[]."""
    attachments: list[tuple[str, str]] = []

    # Timetable API style: attachments.files[]
    att_data = entry.get("attachments") or {}
    for f in att_data.get("files") or []:
        title = f.get("title", "attachment")
        url = f.get("download_url", "")
        if url and not url.startswith("http"):
            url = base_url + url
        if url:
            attachments.append((title, url))

    # Event contributions API style: folders[].attachments[]
    for folder in entry.get("folders") or []:
        for f in folder.get("attachments") or []:
            title = f.get("title", "attachment")
            url = f.get("download_url", "")
            if url and not url.startswith("http"):
                url = base_url + url
            if url:
                attachments.append((title, url))

    return attachments


def contribution_from_json(entry: dict, base_url: str = "") -> Contribution:
    """Build a Contribution from a timetable entry."""
    speakers = []
    for person in entry.get("presenters", []):
        name = person.get("name") or f"{person.get('first_name', '')} {person.get('last_name', '')}".strip()
        if name:
            speakers.append(name)

    attachments = _parse_attachments(entry, base_url)

    return Contribution(
        title=entry.get("title", ""),
        start_dt=parse_indico_datetime(entry["startDate"]),
        end_dt=parse_indico_datetime(entry["endDate"]),
        speakers=speakers,
        attachments=attachments,
    )


def event_from_json(data: dict) -> IndicoEvent:
    """Build an IndicoEvent from the JSON returned by the Indico HTTP Export API."""
    return IndicoEvent(
        id=int(data["id"]),
        title=data.get("title", ""),
        url=data.get("url", ""),
        start_dt=parse_indico_datetime(data["startDate"]),
        end_dt=parse_indico_datetime(data["endDate"]),
        timezone=data["startDate"]["tz"],
        description=data.get("description", ""),
        location=data.get("location", ""),
        category=data.get("category", ""),
        category_id=int(data.get("categoryId", 0)),
        event_type=data.get("type", ""),
    )
