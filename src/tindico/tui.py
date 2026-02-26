import re
import subprocess
from contextlib import nullcontext
from dataclasses import dataclass, field
from enum import Enum, auto

import requests

from rich.style import Style
from rich.text import Text
from textual import work
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.screen import ModalScreen
from textual.widgets import DataTable, Footer, Header, Input, OptionList, Static
from textual.widgets.option_list import Option

from .api import get_category_events, get_category_info, get_favorite_events, get_timetable
from .calendar_sync import (
    find_calendar_events,
    open_in_calendar,
    set_event_url,
    warm_event_store,
)
from .models import Contribution, IndicoEvent

DIM = Style(dim=True)
DIM_ITALIC = Style(dim=True, italic=True)


def _escape_rich(text: str) -> str:
    """Escape square brackets so Rich doesn't interpret them as markup."""
    return text.replace("[", "\\[")


class ViewMode(Enum):
    FAVORITES = auto()
    CATEGORY = auto()


@dataclass
class NavEntry:
    view_mode: ViewMode
    category_id: int = 0
    category_name: str = ""
    cursor_row: int = 0


class DetailDivider(Static):
    """Accent-colored divider between the table and detail panel."""

    DEFAULT_CSS = """
    DetailDivider {
        height: 0;
        border-top: solid $accent;
        background: $surface;
    }
    """

    def __init__(self) -> None:
        super().__init__("")


class DetailPanel(OptionList):
    """Panel showing timetable/contributions for the highlighted event as a selectable list."""

    DEFAULT_CSS = """
    DetailPanel {
        height: 10;
        border: none;
        padding: 0 1;
        scrollbar-size: 0 0;
    }
    DetailPanel:focus {
        border: none;
    }
    """

    # Fixed overhead: header(1) + footer(1) + status bar(1) + divider border(1) = 4 lines
    _FIXED_LINES = 4
    _MIN_HEIGHT = 1
    _MAX_HEIGHT = 10
    # Minimum lines reserved for the top panel (DataTable)
    _MIN_TOP = 3

    @staticmethod
    def height_for_terminal(total_lines: int) -> int:
        """Return a detail-panel height between 1 and 10, scaled to terminal size.

        Prioritises the top panel: it always gets at least _MIN_TOP lines.
        """
        available = total_lines - DetailPanel._FIXED_LINES
        top_minimum = DetailPanel._MIN_TOP
        max_for_bottom = max(available - top_minimum, DetailPanel._MIN_HEIGHT)
        # Give roughly 25% of available space to the detail panel
        ideal = int(available * 0.25)
        return max(DetailPanel._MIN_HEIGHT, min(ideal, DetailPanel._MAX_HEIGHT, max_for_bottom))

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
        prev_date = None
        for i, c in enumerate(contributions):
            date_key = c.start_dt.date()
            if prev_date is not None and date_key != prev_date:
                day_label = Text(
                    f"── {c.start_dt.strftime('%A %b %d').replace(' 0', ' ')} ──",
                    style=DIM,
                )
                self.add_option(Option(day_label, disabled=True))
            prev_date = date_key
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
        height: 1;
        color: $text-muted;
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


class CalendarEventPicker(ModalScreen[tuple | None]):
    """Modal screen to pick a calendar event to update its URL."""

    DEFAULT_CSS = """
    CalendarEventPicker {
        align: center middle;
    }
    CalendarEventPicker OptionList {
        width: 70;
        height: auto;
        max-height: 20;
        border: solid $accent;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("right", "confirm", "Confirm", priority=True),
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, candidates: list[dict], indico_start) -> None:
        super().__init__()
        self._candidates = candidates
        self._indico_start = indico_start

    def compose(self) -> ComposeResult:
        ol = OptionList()
        event_time = self._indico_start.replace(second=0, microsecond=0)
        past_exact = False
        for i, c in enumerate(self._candidates):
            is_exact = c["start"].replace(second=0, microsecond=0) == event_time
            if not is_exact and not past_exact:
                past_exact = True
                ol.add_option(Option(
                    Text("───── other events ─────", style=DIM),
                    disabled=True,
                ))
            time_str = c["start"].strftime("%H:%M")
            label = Text()
            label.append(time_str, style="bold")
            label.append("  ")
            label.append(c["title"])
            label.append(f"  [{c['calendar']}]", style=DIM_ITALIC)
            ol.add_option(Option(label, id=f"cal_{i}"))
        ol.highlighted = 0
        yield ol

    def _dismiss_with(self, idx: int) -> None:
        c = self._candidates[idx]
        self.dismiss((c["ek_event_id"], c["ek_start_ts"]))

    def action_confirm(self) -> None:
        ol = self.query_one(OptionList)
        if ol.highlighted is not None:
            option = ol.get_option_at_index(ol.highlighted)
            if option.id and option.id.startswith("cal_"):
                self._dismiss_with(int(option.id.split("_")[1]))
                return
        self.dismiss(None)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        if event.option_id and event.option_id.startswith("cal_"):
            self._dismiss_with(int(event.option_id.split("_")[1]))

    def action_cancel(self) -> None:
        self.dismiss(None)


class RegexFilterScreen(ModalScreen[str | None]):
    """Modal for entering a regex filter pattern."""

    DEFAULT_CSS = """
    RegexFilterScreen {
        align: center middle;
    }
    RegexFilterScreen Input {
        width: 60;
        border: solid $accent;
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("escape", "cancel", "Cancel"),
    ]

    def __init__(self, current: str = "") -> None:
        super().__init__()
        self._current = current

    def compose(self) -> ComposeResult:
        yield Input(value=self._current, placeholder="Regex filter (title/category)")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        self.dismiss(event.value)

    def action_cancel(self) -> None:
        self.dismiss(None)


class IndicoApp(App):
    """CERN Indico Terminal UI."""

    TITLE = "tindico"
    CSS = """
    DataTable {
        height: 1fr;
        scrollbar-size: 0 0;
    }
    DataTable LoadingIndicator, DetailPanel LoadingIndicator {
        background: $surface;
    }
    """

    BINDINGS = [
        Binding("left", "navigate_parent", "Parent", priority=True),
        Binding("right", "open", "Open", priority=True),
        Binding("slash", "regex_filter", "Filter"),
        Binding("c", "sync_calendar", "Calendar Export"),
        Binding("u", "update_url", "Update URL"),
        Binding("escape", "back_to_favorites", "Back", priority=True),
        Binding("tab", "toggle_focus", show=False),
        Binding("q", "quit", "Quit"),
    ]

    SEPARATOR_KEY_PREFIX = "_sep_"
    SUBCAT_KEY_PREFIX = "_subcat_"

    _TABLE_COLUMNS = [
        ("Day", 3),
        ("Date", 6),
        ("Time", 5),
        ("Title", 42),
        ("Category", 20),
    ]

    @property
    def _accent_hex(self) -> str:
        """Get the current theme's accent color as a hex string."""
        cs = self.current_theme.to_color_system()
        return cs.accent.hex

    def _setup_table_columns(self, table: DataTable) -> None:
        """Clear and re-add standard columns to the table."""
        table.clear(columns=True)
        for name, width in self._TABLE_COLUMNS:
            table.add_column(name, width=width)

    def _add_separator_row(self, table: DataTable, key: str) -> None:
        """Add a dim separator row to the table."""
        table.add_row(
            *(Text("─" * w, style=DIM) for _name, w in self._TABLE_COLUMNS),
            key=key,
        )

    def _add_event_row(
        self, table: DataTable, ev: IndicoEvent, first_of_day: bool, accent: Style,
    ) -> str:
        """Add an event row to the table and return its row key."""
        row_key = str(ev.id)
        table.add_row(
            Text(ev.start_dt.strftime("%a"), style=DIM) if first_of_day else Text(""),
            Text(ev.start_dt.strftime("%b %d").replace(" 0", "  ")) if first_of_day else Text(""),
            Text(ev.start_dt.strftime("%H:%M")),
            Text(ev.title[:42], style=accent),
            Text(ev.category[:20], style=DIM_ITALIC),
            key=row_key,
        )
        self._row_key_to_event[row_key] = ev
        return row_key

    def _open_url(self, url: str, label: str = "") -> None:
        """Open a URL in the default browser and update the status bar."""
        subprocess.run(["open", url])
        self.query_one(StatusBar).update(f"Opened {label or url}")

    def __init__(self) -> None:
        super().__init__()
        self.events: list[IndicoEvent] = []
        self._row_key_to_event: dict[str, IndicoEvent] = {}
        self._timetable_cache: dict[int, list[Contribution]] = {}
        self._category_events_cache: dict[int, list[IndicoEvent]] = {}
        self._category_info_cache: dict[int, dict] = {}
        self._current_detail_event_id: int | None = None
        self._nav_stack: list[NavEntry] = [NavEntry(ViewMode.FAVORITES)]
        self._subcat_names: dict[int, str] = {}
        self._update_url_event: IndicoEvent | None = None
        self._regex_filter: str = ""

    @property
    def _current_nav(self) -> NavEntry:
        return self._nav_stack[-1]

    @property
    def _view_mode(self) -> ViewMode:
        return self._current_nav.view_mode

    @property
    def _category_id(self) -> int:
        return self._current_nav.category_id

    @property
    def _category_name(self) -> str:
        return self._current_nav.category_name

    def compose(self) -> ComposeResult:
        yield Header()
        yield DataTable(cursor_type="row")
        yield DetailDivider()
        yield DetailPanel()
        yield StatusBar("Loading...")
        yield Footer()

    def on_mount(self) -> None:
        warm_event_store()
        table = self.query_one(DataTable)
        for name, width in self._TABLE_COLUMNS:
            table.add_column(name, width=width)
        self._sync_detail_height()
        self._load_events()

    def on_resize(self) -> None:
        self._sync_detail_height()

    def _sync_detail_height(self) -> None:
        """Adjust the detail panel height to fit the current terminal size."""
        panel = self.query_one(DetailPanel)
        panel.styles.height = DetailPanel.height_for_terminal(self.size.height)

    def check_action(self, action: str, parameters: tuple) -> bool | None:
        """Disable arrow-key actions when a text input modal is open."""
        if action in ("navigate_parent", "open") and isinstance(self.screen, RegexFilterScreen):
            return False
        return True

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
        # Save cursor from current nav before resetting
        saved_cursor = self._nav_stack[0].cursor_row if self._nav_stack else 0
        self._nav_stack = [NavEntry(ViewMode.FAVORITES, cursor_row=saved_cursor)]
        self.sub_title = ""
        table = self.query_one(DataTable)
        self._setup_table_columns(table)
        self._row_key_to_event = {}
        self._current_detail_event_id = None
        self.query_one(DetailPanel).set_message("No event selected")

        accent = Style(color=self._accent_hex)
        regex = self._compile_regex_filter()
        prev_date = None
        shown = 0
        for ev in self.events:
            if regex and not (regex.search(ev.title) or regex.search(ev.category)):
                continue
            shown += 1
            date_key = ev.start_dt.strftime("%Y-%m-%d")
            first_of_day = date_key != prev_date
            if prev_date is not None and first_of_day:
                self._add_separator_row(table, f"{self.SEPARATOR_KEY_PREFIX}{date_key}")
            prev_date = date_key
            self._add_event_row(table, ev, first_of_day, accent)

        status = self.query_one(StatusBar)
        if regex:
            status.update(f"{shown}/{len(self.events)} events matching /{self._regex_filter}/")
        else:
            status.update(f"Loaded {len(self.events)} events")

        # Restore cursor position
        if saved_cursor > 0:
            try:
                table.move_cursor(row=saved_cursor)
            except (IndexError, KeyError):
                pass

    def on_data_table_row_highlighted(self, event: DataTable.RowHighlighted) -> None:
        if event.row_key is None:
            return
        key = event.row_key.value
        if key.startswith(self.SEPARATOR_KEY_PREFIX):
            table = self.query_one(DataTable)
            row = table.cursor_row
            # Skip in the direction we were moving (compare to previous position)
            prev = getattr(self, "_prev_cursor_row", 0)
            direction = 1 if row >= prev else -1
            target = row + direction
            if 0 <= target < len(table.ordered_rows):
                table.move_cursor(row=target)
            return
        self._prev_cursor_row = self.query_one(DataTable).cursor_row
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
            panel.loading = True
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
        if self._current_detail_event_id == event_id:
            self.call_from_thread(self._set_panel_contributions, contributions)
        else:
            self.call_from_thread(self._set_panel_loading, False)

    def _set_panel_contributions(self, contributions: list[Contribution]) -> None:
        panel = self.query_one(DetailPanel)
        panel.loading = False
        panel.set_contributions(contributions)

    def _selected_event(self) -> IndicoEvent | None:
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            return None
        row_key = table.ordered_rows[table.cursor_row].key.value
        return self._row_key_to_event.get(row_key)

    def on_data_table_row_selected(self, event: DataTable.RowSelected) -> None:
        ev = self._row_key_to_event.get(event.row_key.value)
        if ev:
            self._open_url(ev.url)

    # -- Actions --------------------------------------------------------

    def _save_cursor(self) -> None:
        """Save the current cursor row into the top nav entry."""
        table = self.query_one(DataTable)
        self._current_nav.cursor_row = table.cursor_row or 0

    def _pop_to_previous_view(self) -> None:
        """Restore the view from the current top of the nav stack after a pop."""
        saved_cursor = self._current_nav.cursor_row
        if self._view_mode == ViewMode.FAVORITES:
            self._restore_favorites_view()
        else:
            cat_id = self._category_id
            if cat_id in self._category_events_cache:
                self._populate_category_table(
                    self._category_events_cache[cat_id], self._category_name
                )
                try:
                    self.query_one(DataTable).move_cursor(row=saved_cursor)
                except (IndexError, KeyError):
                    pass

    def _push_category(self, category_id: int, category_name: str, focus_event_id: int = 0) -> None:
        """Save cursor, push a new category onto the nav stack, and load it."""
        self._save_cursor()
        self._regex_filter = ""
        self._nav_stack.append(NavEntry(ViewMode.CATEGORY, category_id, category_name))
        self._load_category_events(category_id, category_name, focus_event_id)

    def action_navigate_parent(self) -> None:
        """Left arrow: navigate to parent category."""
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss(None)
            return
        if self._view_mode == ViewMode.FAVORITES:
            ev = self._selected_event()
            if ev is None or ev.category_id == 0:
                self.query_one(StatusBar).update("No category for this event")
                return
            # Go to the event's own category
            self._push_category(ev.category_id, ev.category, ev.id)
        else:
            self._navigate_to_parent_of(self._category_id, self._category_name)

    @work(exclusive=True, group="cat_info", thread=True)
    def _navigate_to_parent_of(self, category_id: int, category_name: str, focus_event_id: int = 0) -> None:
        """Fetch category info and navigate to its parent."""
        status = self.query_one(StatusBar)
        self.call_from_thread(status.update, "Loading parent category...")
        self.call_from_thread(self._set_table_loading, True)
        self.call_from_thread(self._set_panel_loading, True)

        def _restore() -> None:
            self.call_from_thread(self._set_table_loading, False)
            self.call_from_thread(self._set_panel_loading, False)
            self.call_from_thread(status.update, "")

        try:
            info = self._fetch_category_info(category_id)
        except Exception as e:
            _restore()
            self.call_from_thread(self.notify, f"Cannot access parent category: {e}", severity="error")
            return
        if info["parent_id"] is None:
            _restore()
            self.call_from_thread(self.notify, "Already at top-level category")
            return
        # Check we can actually access the parent before navigating
        try:
            self._fetch_category_info(info["parent_id"])
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 403:
                _restore()
                self.call_from_thread(
                    self.notify,
                    f"Access denied to '{info['parent_name']}'",
                    severity="error",
                )
                return
            raise
        self.call_from_thread(
            self._push_category, info["parent_id"], info["parent_name"], focus_event_id
        )

    def _fetch_category_info(self, category_id: int) -> dict:
        """Get category info, using cache if available."""
        if category_id in self._category_info_cache:
            return self._category_info_cache[category_id]
        info = get_category_info(category_id)
        self._category_info_cache[category_id] = info
        return info

    def action_open(self) -> None:
        """Right arrow: open event in browser or open subcategory."""
        if isinstance(self.screen, CalendarEventPicker):
            self.screen.action_confirm()
            return
        if isinstance(self.screen, AttachmentPicker):
            self.screen.select_highlighted()
            return
        if self.query_one(DetailPanel).has_focus:
            self.action_open_material()
            return
        table = self.query_one(DataTable)
        if table.cursor_row is None:
            return
        row_key = table.ordered_rows[table.cursor_row].key.value
        # Check if it's a subcategory row
        if row_key.startswith(self.SUBCAT_KEY_PREFIX):
            subcat_id = int(row_key[len(self.SUBCAT_KEY_PREFIX):])
            subcat_name = self._subcat_names.get(subcat_id, "")
            self._push_category(subcat_id, subcat_name)
            return
        # Otherwise open event in browser
        ev = self._row_key_to_event.get(row_key)
        if ev:
            self._open_url(ev.url)

    def _set_table_loading(self, loading: bool) -> None:
        self.query_one(DataTable).loading = loading

    def _set_panel_loading(self, loading: bool) -> None:
        self.query_one(DetailPanel).loading = loading

    @work(exclusive=True, group="load_view", thread=True)
    def _load_category_events(
        self, category_id: int, category_name: str, focus_event_id: int = 0
    ) -> None:
        self.call_from_thread(
            self.query_one(StatusBar).update, f"Loading category '{_escape_rich(category_name)}'..."
        )
        self.call_from_thread(self._set_table_loading, True)

        if category_id in self._category_events_cache:
            events = self._category_events_cache[category_id]
        else:
            try:
                events = get_category_events(category_id)
            except Exception as e:
                self.call_from_thread(self._set_table_loading, False)
                # Pop the failed nav entry and stay where we were
                if len(self._nav_stack) > 1:
                    self._nav_stack.pop()
                self.call_from_thread(self._pop_to_previous_view)
                self.call_from_thread(
                    self.notify,
                    f"Cannot access category: {e}",
                    severity="error",
                )
                return
            self._category_events_cache[category_id] = events

        # Show table immediately (without subcategories if info not cached yet)
        self.call_from_thread(
            self._populate_category_table, events, category_name, focus_event_id
        )

        # Then fetch category info and re-render if subcategories were found
        if category_id not in self._category_info_cache:
            try:
                info = self._fetch_category_info(category_id)
                if info.get("subcategories"):
                    self.call_from_thread(
                        self._populate_category_table, events, category_name, focus_event_id
                    )
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 403 and not events:
                    # Export API returned empty + info returned 403 → restricted
                    if len(self._nav_stack) > 1:
                        self._nav_stack.pop()
                    self.call_from_thread(self._pop_to_previous_view)
                    self.call_from_thread(
                        self.notify,
                        f"Access denied to category '{category_name}'",
                        severity="error",
                    )
            except Exception as e:
                self.call_from_thread(
                    self.query_one(StatusBar).update, f"Category info error: {e}"
                )

    def _populate_category_table(
        self,
        events: list[IndicoEvent],
        category_name: str,
        focus_event_id: int = 0,
    ) -> None:
        """Rebuild DataTable for category view with subcategories and events."""
        self.query_one(DataTable).loading = False
        self.query_one(DetailPanel).loading = False
        self._current_nav.view_mode = ViewMode.CATEGORY
        self.sub_title = f"Category: {_escape_rich(category_name)}"
        table = self.query_one(DataTable)
        self._setup_table_columns(table)
        self._row_key_to_event = {}
        self._subcat_names = {}
        keep_detail = (
            focus_event_id
            and focus_event_id == self._current_detail_event_id
            and focus_event_id in self._timetable_cache
        )
        if not keep_detail:
            self._current_detail_event_id = None
            self.query_one(DetailPanel).set_message("No event selected")

        accent = Style(color=self._accent_hex)
        cat_id = self._category_id
        row_index = 0
        focus_row = 0

        # Suppress highlight events during rebuild when preserving detail panel
        prevent = table.prevent(DataTable.RowHighlighted) if keep_detail else nullcontext()
        regex = self._compile_regex_filter()
        with prevent:
            # Add subcategory rows at the top
            info = self._category_info_cache.get(cat_id)
            subcats = info.get("subcategories", []) if info else []
            shown_subcats = False
            for sub in subcats:
                sub_id = sub["id"]
                sub_title = sub["title"]
                self._subcat_names[sub_id] = sub_title
                if regex and not regex.search(sub_title):
                    continue
                shown_subcats = True
                row_key = f"{self.SUBCAT_KEY_PREFIX}{sub_id}"
                table.add_row(
                    Text(""),
                    Text(""),
                    Text("  →", style="bold"),
                    Text(sub_title[:42], style=Style(color=self._accent_hex, bold=True)),
                    Text("subcategory", style=DIM_ITALIC),
                    key=row_key,
                )
                row_index += 1

            # Add separator between subcategories and events
            if shown_subcats:
                self._add_separator_row(table, f"{self.SEPARATOR_KEY_PREFIX}subcats")
                row_index += 1

            prev_date = None
            shown = 0
            for ev in events:
                if regex and not (regex.search(ev.title) or regex.search(ev.category)):
                    continue
                shown += 1
                date_key = ev.start_dt.strftime("%Y-%m-%d")
                first_of_day = date_key != prev_date
                if prev_date is not None and first_of_day:
                    self._add_separator_row(table, f"{self.SEPARATOR_KEY_PREFIX}{date_key}")
                    row_index += 1
                prev_date = date_key
                self._add_event_row(table, ev, first_of_day, accent)
                if ev.id == focus_event_id:
                    focus_row = row_index
                row_index += 1

        if focus_row > 0:
            table.move_cursor(row=focus_row)

        status = self.query_one(StatusBar)
        if regex:
            status.update(
                f"{shown}/{len(events)} events matching /{self._regex_filter}/ in '{_escape_rich(category_name)}'"
            )
        else:
            status.update(
                f"{len(events)} events in '{_escape_rich(category_name)}'"
            )

    def action_back_to_favorites(self) -> None:
        if isinstance(self.screen, ModalScreen):
            self.screen.dismiss(None)
            return
        if self._view_mode == ViewMode.FAVORITES:
            return
        self._regex_filter = ""
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
        self._update_url_event = event
        try:
            candidates = find_calendar_events(event)
        except Exception as e:
            status.update(f"URL update error: {e}")
            return
        if not candidates:
            status.update("No calendar events found")
            return
        self.push_screen(
            CalendarEventPicker(candidates, event.start_dt),
            self._on_calendar_event_picked,
        )

    def _on_calendar_event_picked(self, result: tuple | None) -> None:
        status = self.query_one(StatusBar)
        if result is None:
            status.update("Cancelled")
            return
        event_id, start_ts = result
        event = self._update_url_event
        try:
            ok = set_event_url(event_id, start_ts, event.url)
            if ok:
                status.update("Updated calendar event URL")
            else:
                status.update("Failed to update calendar event")
        except Exception as e:
            status.update(f"URL update error: {e}")

    def action_toggle_focus(self) -> None:
        """Toggle focus between DataTable and the detail panel OptionList."""
        table = self.query_one(DataTable)
        panel = self.query_one(DetailPanel)
        if table.has_focus:
            panel.focus()
        else:
            table.focus()

    def _compile_regex_filter(self) -> re.Pattern | None:
        """Compile the current regex filter, returning None if empty or invalid."""
        if not self._regex_filter:
            return None
        try:
            return re.compile(self._regex_filter, re.IGNORECASE)
        except re.error:
            return None

    def action_regex_filter(self) -> None:
        self.push_screen(
            RegexFilterScreen(self._regex_filter),
            self._on_regex_entered,
        )

    def _on_regex_entered(self, result: str | None) -> None:
        if result is None:
            return
        # Validate regex before applying
        if result:
            try:
                re.compile(result)
            except re.error as e:
                self.query_one(StatusBar).update(f"Invalid regex: {e}")
                return
        self._regex_filter = result
        if self._view_mode == ViewMode.FAVORITES:
            self._restore_favorites_view()
        elif self._category_id and self._category_id in self._category_events_cache:
            self._populate_category_table(
                self._category_events_cache[self._category_id],
                self._category_name,
            )

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
            self._open_url(url, _title)
        else:
            self.push_screen(
                AttachmentPicker(contrib.attachments),
                callback=self._on_attachment_picked,
            )

    def _on_attachment_picked(self, url: str | None) -> None:
        if url is None:
            return
        self._open_url(url, "attachment")
