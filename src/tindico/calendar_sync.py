import subprocess
import tempfile
from pathlib import Path

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

    description = event.description
    if event.url:
        description = f"{event.url}\n\n{description}" if description else event.url
    vevent.add("description", description)

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


def update_existing_event_url(event: IndicoEvent) -> bool:
    """Use AppleScript to find an existing Calendar.app event and add the Indico URL.

    Searches by title and start date. Returns True if an event was found and updated.
    """
    date_str = event.start_dt.strftime("%B %d, %Y")
    url = event.url

    script = f'''
tell application "Calendar"
    set matchFound to false
    repeat with c in calendars
        set evts to (every event of c whose summary is "{event.title}" and start date >= date "{date_str} 00:00:00" and start date < date "{date_str} 23:59:59")
        repeat with e in evts
            set url of e to "{url}"
            set matchFound to true
        end repeat
    end repeat
    return matchFound
end tell
'''

    result = subprocess.run(
        ["osascript", "-e", script],
        capture_output=True,
        text=True,
        timeout=15,
    )
    return result.returncode == 0 and "true" in result.stdout.lower()
