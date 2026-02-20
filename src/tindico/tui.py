import subprocess
from enum import Enum, auto

from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, OptionList, Static
from textual.widgets.option_list import Option

from .api import get_category_events, get_favorite_events, get_timetable
from .calendar_sync import open_in_calendar, update_existing_event_url
from .models import Contribution, IndicoEvent

DIM = Style(dim=True)
DIM_ITALIC = Style(dim=True, italic=True)


def _escape_rich(text: str) -> str:
    """Escape square brackets so Rich doesn't interpret them as markup."""
    return text.replace("[", "\\[")


class ViewMode(Enum):
    FAVORITES = auto()
    CATEGORY = auto()


class DetailPanel(OptionList):
    """Panel showing timetable/contributions for the highlighted event as a selectable list."""

    DEFAULT_CSS = """
    DetailPanel {
        height: 8;
        border: none;
        border-top: solid $accent;
        padding: 0 1;
    }
    DetailPanel:focus {
        border: none;
        border-top: solid $accent;
    }
    """

    def __init__(self) -> None:
        super().__init__(Option("No event selected", disabled=True))
        self._contributions: dict[str, Contribution] = {}

    def set_message(self, text: str) -> None:
        """Show a simple message (Loading..., No event selected, etc.)."""
        self._contributions = {}
        self.clear_options()
        self.add_option(Option(text, disabled=True))

    def set_contributions(self, contributions: list[Contribution]) -> None:
        """Populate the list with contributions. Those with attachments get a * suffix."""
        self._contributions = {}
        self.clear_options()
        if not contributions:
            self.add_option(Option("No contributions", disabled=True))
            return
        accent_hex = self.app.current_theme.to_color_system().accent.hex
        for i, c in enumerate(contributions):
            time_str = c.start_dt.strftime("%H:%M")
            speakers = ", ".join(c.speakers)
            label = Text()
            label.append(time_str, style="bold")
            label.append("  ")
            label.append(c.title, style=Style(color=accent_hex))
            if speakers:
                label.append(f" [{speakers}]", style=DIM_ITALIC)
            if c.attachments:
                label.append(" ●", style="bold")
            opt_id = f"contrib_{i}"
            self.add_option(Option(label, id=opt_id))
            self._contributions[opt_id] = c

    def on_focus(self) -> None:
        if self._contributions and self.highlighted is None:
            self.highlighted = 0

    def selected_contribution(self) -> Contribution | None:
        """Return the currently highlighted contribution, if any."""
        if self.highlighted is None:
            return None
        option = self.get_option_at_index(self.highlighted)
        return self._contributions.get(option.id)


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


class AttachmentPicker(ModalScreen[str | None]):
    """Modal screen to pick from multiple attachments."""

    DEFAULT_CSS = """
    AttachmentPicker {
        align: center middle;
    }
    AttachmentPicker OptionList {
        width: 60;
        height: auto;
        max-height: 16;
        border: solid $accent;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, attachments: list[tuple[str, str]]) -> None:
        super().__init__()
        self._attachments = attachments

    def compose(self) -> ComposeResult:
        ol = OptionList()
        for i, (title, _url) in enumerate(self._attachments):
            ol.add_option(Option(title, id=f"att_{i}"))
        ol.highlighted = 0
        yield ol

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = int(event.option_id.split("_")[1])
        _title, url = self._attachments[idx]
        self.dismiss(url)

    def select_highlighted(self) -> None:
        """Open the currently highlighted attachment."""
        ol = self.query_one(OptionList)
        if ol.highlighted is not None:
            _title, url = self._attachments[ol.highlighted]
            self.dismiss(url)

    def action_cancel(self) -> None:
        self.dismiss(None)


class IndicoApp(App):
    """CERN Indico Terminal UI."""

    TITLE = "tindico"
    CSS = """
    DataTable {
        height: 1fr;
    }
    """

    BINDINGS = [
        Binding("left", "category_drilldown", "Category", priority=True),
        Binding("right", "open_browser", "Open in Browser", priority=True),
        Binding("c", "sync_calendar", "Calendar Sync"),
        Binding("u", "update_url", "Update URL"),
        Binding("escape", "back_to_favorites", "Back", priority=True),
        Binding("tab", "toggle_focus", show=False),
        Binding("q", "quit", "Quit"),
    ]

    SEPARATOR_KEY_PREFIX = "_sep_"

    @property
    def _accent_hex(self) -> str:
        """Get the current theme's accent color as a hex string."""
        cs = self.current_theme.to_color_system()
        return cs.accent.hex

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
        table.add_column("Day", width=3)
        table.add_column("Date", width=6)
        table.add_column("Time", width=5)
        table.add_column("Title", width=42)
        table.add_column("Category", width=20)
        self._load_events()

    def watch_theme(self, old_value: str, new_value: str) -> None:
        """Re-render the table when the theme changes so colors update."""
        if self._view_mode == ViewMode.FAVORITES:
            self._restore_favorites_view()
        elif self._category_id and self._category_id in self._category_events_cache:
            self._populate_category_table(
                self._category_events_cache[self._category_id],
                self._category_name,
            )

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
        table.add_column("Day", width=3)
        table.add_column("Date", width=6)
        table.add_column("Time", width=5)
        table.add_column("Title", width=42)
        table.add_column("Category", width=20)
        self._row_key_to_event = {}
        self._current_detail_event_id = None
        self.query_one(DetailPanel).set_message("No event selected")

        accent = Style(color=self._accent_hex)
        prev_date = None
        for ev in self.events:
            date_str = ev.start_dt.strftime("%b %d").replace(" 0", "  ")
            first_of_day = date_str != prev_date
            if prev_date is not None and first_of_day:
                sep_key = f"{self.SEPARATOR_KEY_PREFIX}{date_str}"
                table.add_row(
                    Text("─" * 3, style=DIM),
                    Text("─" * 6, style=DIM),
                    Text("─" * 5, style=DIM),
                    Text("─" * 42, style=DIM),
                    Text("─" * 20, style=DIM),
                    key=sep_key,
                )
            prev_date = date_str
            row_key = str(ev.id)
            table.add_row(
                Text(ev.start_dt.strftime("%a"), style=DIM) if first_of_day else Text(""),
                Text(date_str) if first_of_day else Text(""),
                Text(ev.start_dt.strftime("%H:%M")),
                Text(ev.title[:42], style=accent),
                Text(ev.category[:20], style=DIM_ITALIC),
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
            panel.set_contributions(self._timetable_cache[ev.id])
        else:
            panel.set_message("Loading...")
            self._fetch_timetable(ev.id)

    @work(exclusive=True, group="timetable", thread=True)
    def _fetch_timetable(self, event_id: int) -> None:
        try:
            contributions = get_timetable(event_id)
        except Exception as e:
            contributions = []
            self.call_from_thread(
                self.query_one(StatusBar).update, f"Timetable error: {e}"
            )
        self._timetable_cache[event_id] = contributions
        # Only update panel if still viewing this event
        if self._current_detail_event_id == event_id:
            self.call_from_thread(
                self.query_one(DetailPanel).set_contributions,
                contributions,
            )

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
        table.add_column("Day", width=3)
        table.add_column("Date", width=6)
        table.add_column("Time", width=5)
        table.add_column("Title", width=42)
        self._row_key_to_event = {}
        self._current_detail_event_id = None
        self.query_one(DetailPanel).set_message("No event selected")

        accent = Style(color=self._accent_hex)
        focus_row = 0
        row_index = 0
        prev_date = None
        for ev in events:
            date_str = ev.start_dt.strftime("%b %d").replace(" 0", "  ")
            first_of_day = date_str != prev_date
            if prev_date is not None and first_of_day:
                sep_key = f"{self.SEPARATOR_KEY_PREFIX}{date_str}"
                table.add_row(
                    Text("─" * 3, style=DIM),
                    Text("─" * 6, style=DIM),
                    Text("─" * 5, style=DIM),
                    Text("─" * 42, style=DIM),
                    key=sep_key,
                )
                row_index += 1
            prev_date = date_str
            row_key = str(ev.id)
            table.add_row(
                Text(ev.start_dt.strftime("%a"), style=DIM) if first_of_day else Text(""),
                Text(date_str) if first_of_day else Text(""),
                Text(ev.start_dt.strftime("%H:%M")),
                Text(ev.title[:42], style=accent),
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
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss(None)
            return
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
        if isinstance(self.screen, AttachmentPicker):
            self.screen.select_highlighted()
            return
        if self.query_one(DetailPanel).has_focus:
            self.action_open_material()
            return
        status = self.query_one(StatusBar)
        event = self._selected_event()
        if not event:
            status.update("No event selected")
            return
        subprocess.run(["open", event.url], check=True)
        status.update(f"Opened {event.url}")

    def action_toggle_focus(self) -> None:
        """Toggle focus between DataTable and the detail panel OptionList."""
        table = self.query_one(DataTable)
        panel = self.query_one(DetailPanel)
        if table.has_focus:
            panel.focus()
        else:
            table.focus()

    def action_open_material(self) -> None:
        """Open attachments for the selected contribution."""
        status = self.query_one(StatusBar)
        panel = self.query_one(DetailPanel)
        contrib = panel.selected_contribution()
        if contrib is None:
            status.update("No contribution selected")
            return
        if not contrib.attachments:
            status.update("No attachments")
            return
        if len(contrib.attachments) == 1:
            _title, url = contrib.attachments[0]
            subprocess.run(["open", url], check=True)
            status.update(f"Opened {_title}")
        else:
            self.push_screen(
                AttachmentPicker(contrib.attachments),
                callback=self._on_attachment_picked,
            )

    def _on_attachment_picked(self, url: str | None) -> None:
        if url is None:
            return
        subprocess.run(["open", url], check=True)
        self.query_one(StatusBar).update(f"Opened attachment")
