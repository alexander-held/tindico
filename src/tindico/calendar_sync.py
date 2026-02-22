import subprocess
import tempfile
import threading
from datetime import datetime, timezone
from pathlib import Path

import EventKit
from Foundation import NSDate, NSURL
from icalendar import Calendar, Event, vText

from .models import IndicoEvent


def create_ics(event: IndicoEvent) -> Path:
    """Generate a .ics file for the given event and return its path."""
    cal = Calendar()
    cal.add("prodid", "-//tindico//indico.cern.ch//")
    cal.add("version", "2.0")

    vevent = Event()
    vevent.add("uid", f"indico-event-{event.id}@indico.cern.ch")
    vevent.add("summary", event.title)
    vevent.add("dtstart", event.start_dt)
    vevent.add("dtend", event.end_dt)
    vevent.add("url", event.url)
    vevent.add("location", vText(event.location))
    vevent.add("description", event.description)

    cal.add_component(vevent)

    fd, name = tempfile.mkstemp(suffix=".ics", prefix=f"indico-{event.id}-")
    path = Path(name)
    with open(fd, "wb") as f:
        f.write(cal.to_ical())
    return path


def open_in_calendar(event: IndicoEvent) -> Path:
    """Create a .ics file and open it with macOS Calendar."""
    path = create_ics(event)
    subprocess.run(["open", str(path)], check=True)
    return path


_cached_store = None


def _get_event_store():
    """Get an authorized EKEventStore with full access, cached after first call."""
    global _cached_store
    if _cached_store is not None:
        return _cached_store

    store = EventKit.EKEventStore.alloc().init()
    done = threading.Event()
    granted = [None]

    def handler(ok, err):
        granted[0] = ok
        done.set()

    # macOS 14+: request full (read+write) access; older: legacy API grants both
    if hasattr(store, "requestFullAccessToEventsWithCompletion_"):
        store.requestFullAccessToEventsWithCompletion_(handler)
    else:
        store.requestAccessToEntityType_completion_(
            EventKit.EKEntityTypeEvent, handler
        )
    done.wait(timeout=5.0)

    if granted[0]:
        _cached_store = store
        return store
    return None


def warm_event_store():
    """Pre-authorize EventKit in a background thread."""
    threading.Thread(target=_get_event_store, daemon=True).start()


def find_calendar_events(event: IndicoEvent) -> list[dict]:
    """Find calendar events on the same day as the Indico event.

    Returns a list of dicts sorted with exact start-time matches first,
    then remaining events by start time:
        {"title": str, "start": datetime, "calendar": str,
         "existing_url": str | None, "ek_event_id": str}
    """
    store = _get_event_store()
    if store is None:
        return []

    start_of_day = event.start_dt.replace(hour=0, minute=0, second=0)
    end_of_day = event.start_dt.replace(hour=23, minute=59, second=59)
    ns_start = NSDate.dateWithTimeIntervalSince1970_(start_of_day.timestamp())
    ns_end = NSDate.dateWithTimeIntervalSince1970_(end_of_day.timestamp())

    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        ns_start, ns_end, None
    )
    matches = store.eventsMatchingPredicate_(predicate)
    if not matches:
        return []

    results = []
    for cal_event in matches:
        start_ts = cal_event.startDate().timeIntervalSince1970()
        start_dt = datetime.fromtimestamp(start_ts, tz=timezone.utc).astimezone(
            event.start_dt.tzinfo
        )
        existing_url = cal_event.URL().absoluteString() if cal_event.URL() else None
        results.append({
            "title": str(cal_event.title()),
            "start": start_dt,
            "calendar": str(cal_event.calendar().title()),
            "existing_url": existing_url,
            "ek_event_id": str(cal_event.eventIdentifier()),
            "ek_start_ts": start_ts,
        })

    event_time = event.start_dt.replace(second=0, microsecond=0)
    exact = [r for r in results if r["start"].replace(second=0, microsecond=0) == event_time]
    rest = [r for r in results if r["start"].replace(second=0, microsecond=0) != event_time]
    exact.sort(key=lambda r: r["start"])
    rest.sort(key=lambda r: r["start"])

    return exact + rest


def set_event_url(event_id: str, start_ts: float, url: str) -> bool:
    """Look up a specific calendar event occurrence and set its URL.

    Uses event_id + start_ts to find the exact occurrence (important for
    recurring events where eventWithIdentifier_ returns the master event).
    """
    store = _get_event_store()
    if store is None:
        return False

    # Query a narrow window around the occurrence to find the specific instance
    ns_start = NSDate.dateWithTimeIntervalSince1970_(start_ts - 1)
    ns_end = NSDate.dateWithTimeIntervalSince1970_(start_ts + 60)
    predicate = store.predicateForEventsWithStartDate_endDate_calendars_(
        ns_start, ns_end, None
    )
    matches = store.eventsMatchingPredicate_(predicate)

    cal_event = None
    for ev in matches or []:
        if str(ev.eventIdentifier()) == event_id:
            cal_event = ev
            break

    if cal_event is None:
        return False

    cal_event.setURL_(NSURL.URLWithString_(url))
    ok, error = store.saveEvent_span_commit_error_(
        cal_event, EventKit.EKSpanThisEvent, False, None
    )
    if not ok:
        raise RuntimeError(f"Failed to save event: {error}")
    ok, error = store.commit_(None)
    if not ok:
        raise RuntimeError(f"Failed to commit: {error}")
    return True
