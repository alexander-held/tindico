import subprocess
from enum import Enum, auto

from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import ScrollableContainer
from textual.widgets import DataTable, Footer, Header, Static

from .api import get_category_events, get_favorite_events, get_timetable
from .calendar_sync import open_in_calendar, update_existing_event_url
from .models import Contribution, IndicoEvent


def _escape_rich(text: str) -> str:
    """Escape square brackets so Rich doesn't interpret them as markup."""
    return text.replace("[", "\\[")


class ViewMode(Enum):
    FAVORITES = auto()
    CATEGORY = auto()


class DetailPanel(ScrollableContainer):
    """Scrollable panel showing timetable/contributions for the highlighted event."""

    DEFAULT_CSS = """
    DetailPanel {
        height: 8;
        border-top: solid $accent;
        padding: 0 1;
    }
    """

    def compose(self) -> ComposeResult:
        yield Static("No event selected", id="detail-content")

    def update(self, content: str) -> None:
        self.query_one("#detail-content", Static).update(content)
        self.scroll_home(animate=False)


class StatusBar(Static):
    """Bottom status bar for feedback messages."""

    DEFAULT_CSS = """
    StatusBar {
        dock: bottom;
        height: 1;
        background: $accent;
        color: $text;
        padding: 0 1;
    }
    """


class IndicoApp(App):
    """CERN Indico Terminal UI."""

    TITLE = "tindico"
    CSS = """
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("c", "sync_calendar", "Calendar Sync"),
        Binding("u", "update_url", "Update URL"),
        Binding("left", "category_drilldown", "← Category", priority=True),
        Binding("right", "open_browser", "Open in Browser", priority=True),
        Binding("escape", "back_to_favorites", "Back", priority=True),
        Binding("q", "quit", "Quit"),
    ]

    SEPARATOR_KEY_PREFIX = "_sep_"

    events: list[IndicoEvent] = []
    _row_key_to_event: dict[str, IndicoEvent] = {}
    _timetable_cache: dict[int, list[Contribution]] = {}
    _category_events_cache: dict[int, list[IndicoEvent]] = {}
    _current_detail_event_id: int | None = None
    _view_mode: ViewMode = ViewMode.FAVORITES
    _category_id: int = 0
    _category_name: str = ""
    _favorites_cursor_row: int = 0

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row")
        yield DetailPanel()
        yield StatusBar("Loading...")
        yield Footer()

    def on_mount(self) -> None:
        table = self.query_one(DataTable)
        table.add_columns("Day", "Date", "Time", "Title", "Category")
        self._load_events()

    def _load_events(self) -> None:
        status = self.query_one(StatusBar)
        status.update("Loading events...")
        try:
            self.events = get_favorite_events()
        except Exception as e:
            status.update(f"Error: {e}")
            return

        self._timetable_cache = {}
        self._restore_favorites_view()

    def _restore_favorites_view(self) -> None:
        """Rebuild the DataTable with cached favorites events."""
        self._view_mode = ViewMode.FAVORITES
        self.sub_title = ""
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns("Day", "Date", "Time", "Title", "Category")
        self._row_key_to_event = {}
        self._current_detail_event_id = None
        self.query_one(DetailPanel).update("No event selected")

        prev_date = None
        for ev in self.events:
            date_str = ev.start_dt.strftime("%b %d").replace(" 0", "  ")
            if prev_date is not None and date_str != prev_date:
                sep_key = f"{self.SEPARATOR_KEY_PREFIX}{date_str}"
                table.add_row("─" * 3, "─" * 6, "─" * 5, "─" * 42, "─" * 20, key=sep_key)
            prev_date = date_str
            row_key = str(ev.id)
            table.add_row(
                ev.start_dt.strftime("%a"),
                date_str,
                ev.start_dt.strftime("%H:%M"),
                ev.title[:42],
                ev.category[:20],
                key=row_key,
            )
            self._row_key_to_event[row_key] = ev

        status = self.query_one(StatusBar)
        status.update(f"Loaded {len(self.events)} events")

        # Restore cursor position
        if self._favorites_cursor_row > 0:
            try:
                table.move_cursor(row=self._favorites_cursor_row)
            except Exception:
                pass

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        key = event.row_key.value
        if key.startswith(self.SEPARATOR_KEY_PREFIX):
            return
        ev = self._row_key_to_event.get(key)
        if ev is None:
            return
        if ev.id == self._current_detail_event_id:
            return
        self._current_detail_event_id = ev.id
        panel = self.query_one(DetailPanel)
        if ev.id in self._timetable_cache:
            panel.update(self._format_contributions(self._timetable_cache[ev.id]))
        else:
            panel.update("Loading...")
            self._fetch_timetable(ev.id)

    @work(exclusive=True, group="timetable", thread=True)
    def _fetch_timetable(self, event_id: int) -> None:
        try:
            contributions = get_timetable(event_id)
        except Exception:
            contributions = []
        self._timetable_cache[event_id] = contributions
        # Only update panel if still viewing this event
        if self._current_detail_event_id == event_id:
            self.call_from_thread(
                self.query_one(DetailPanel).update,
                self._format_contributions(contributions),
            )

    @staticmethod
    def _format_contributions(contributions: list[Contribution]) -> str:
        if not contributions:
            return "No contributions"
        lines = []
        for c in contributions:
            time_str = c.start_dt.strftime("%H:%M")
            title = _escape_rich(c.title)
            speakers = ", ".join(c.speakers)
            if speakers:
                lines.append(f"{time_str}  {title} \\[{speakers}]")
            else:
                lines.append(f"{time_str}  {title}")
        return "\n".join(lines)

    def _selected_event(self) -> IndicoEvent | None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            return None
        row_key = table.ordered_rows[table.cursor_row].key.value
        return self._row_key_to_event.get(row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        ev = self._row_key_to_event.get(event.row_key.value)
        if ev:
            subprocess.run(["open", ev.url], check=True)
            self.query_one(StatusBar).update(f"Opened {ev.url}")

    # -- Actions --------------------------------------------------------

    def action_category_drilldown(self) -> None:
        if self._view_mode == ViewMode.CATEGORY:
            # Already in category view — left means go back
            self.action_back_to_favorites()
            return
        ev = self._selected_event()
        if ev is None or ev.category_id == 0:
            self.query_one(StatusBar).update("No category for this event")
            return
        # Save cursor position
        table = self.query_one(DataTable)
        self._favorites_cursor_row = table.cursor_row or 0
        self._load_category_events(ev.category_id, ev.category, ev.id)

    @work(exclusive=True, group="load_view", thread=True)
    def _load_category_events(
        self, category_id: int, category_name: str, focus_event_id: int = 0
    ) -> None:
        self._category_id = category_id
        self._category_name = category_name
        self.call_from_thread(
            self.query_one(StatusBar).update, f"Loading category '{_escape_rich(category_name)}'..."
        )

        if category_id in self._category_events_cache:
            events = self._category_events_cache[category_id]
        else:
            try:
                events = get_category_events(category_id)
            except Exception as e:
                self.call_from_thread(
                    self.query_one(StatusBar).update, f"Error: {e}"
                )
                return
            self._category_events_cache[category_id] = events

        self.call_from_thread(
            self._populate_category_table, events, category_name, focus_event_id
        )

    def _populate_category_table(
        self,
        events: list[IndicoEvent],
        category_name: str,
        focus_event_id: int = 0,
    ) -> None:
        """Rebuild DataTable for category view (no Category column)."""
        self._view_mode = ViewMode.CATEGORY
        self.sub_title = f"Category: {_escape_rich(category_name)}"
        table = self.query_one(DataTable)
        table.clear(columns=True)
        table.add_columns("Day", "Date", "Time", "Title")
        self._row_key_to_event = {}
        self._current_detail_event_id = None
        self.query_one(DetailPanel).update("No event selected")

        focus_row = 0
        row_index = 0
        prev_date = None
        for ev in events:
            date_str = ev.start_dt.strftime("%b %d").replace(" 0", "  ")
            if prev_date is not None and date_str != prev_date:
                sep_key = f"{self.SEPARATOR_KEY_PREFIX}{date_str}"
                table.add_row("─" * 3, "─" * 6, "─" * 5, "─" * 50, key=sep_key)
                row_index += 1
            prev_date = date_str
            row_key = str(ev.id)
            table.add_row(
                ev.start_dt.strftime("%a"),
                date_str,
                ev.start_dt.strftime("%H:%M"),
                ev.title[:50],
                key=row_key,
            )
            if ev.id == focus_event_id:
                focus_row = row_index
            row_index += 1
            self._row_key_to_event[row_key] = ev

        if focus_row > 0:
            table.move_cursor(row=focus_row)

        status = self.query_one(StatusBar)
        status.update(
            f"{len(events)} events in '{_escape_rich(category_name)}' | ESC to go back"
        )

    def action_back_to_favorites(self) -> None:
        if self._view_mode == ViewMode.FAVORITES:
            return
        self._restore_favorites_view()

    def action_sync_calendar(self) -> None:
        status = self.query_one(StatusBar)
        event = self._selected_event()
        if not event:
            status.update("No event selected")
            return
        try:
            path = open_in_calendar(event)
            status.update(f"Opened {path.name} in Calendar")
        except Exception as e:
            status.update(f"Calendar sync error: {e}")

    def action_update_url(self) -> None:
        status = self.query_one(StatusBar)
        event = self._selected_event()
        if not event:
            status.update("No event selected")
            return
        try:
            ok = update_existing_event_url(event)
            if ok:
                status.update(f"Updated URL for '{_escape_rich(event.title)}'")
            else:
                status.update(f"No matching calendar event found for '{_escape_rich(event.title)}'")
        except Exception as e:
            status.update(f"URL update error: {e}")

    def action_open_browser(self) -> None:
        status = self.query_one(StatusBar)
        event = self._selected_event()
        if not event:
            status.update("No event selected")
            return
        subprocess.run(["open", event.url], check=True)
        status.update(f"Opened {event.url}")
