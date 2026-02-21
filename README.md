# tindico

*Swipe right on your meetings.*

A terminal UI for browsing [CERN Indico](https://indico.cern.ch) events and syncing them to macOS Calendar.

## Setup

Create an API token with read permissions: https://indico.cern.ch/user/tokens/.

```bash
# Add your Indico API token to .env
echo "INDICO_API_TOKEN=your_token_here" > .env

# Install and run
pixi install
pixi run tindico
```

## Keybindings

| Key | Action |
|-----|--------|
| `←` | Drill down into event's category (±30 days) |
| `→` | Open event in browser / open material (when in detail panel) |
| `Tab` | Toggle focus between event list and detail panel |
| `ESC` | Back to favorites view |
| `c` | Sync event to Calendar (.ics) |
| `u` | Update existing calendar event with Indico URL |
| `q` | Quit |

## macOS Permissions

The `c` and `u` keybindings use AppleScript to interact with Calendar.app. On first use, macOS will prompt you to grant permissions:

- **System Settings > Privacy & Security > Automation** — allow your terminal to control Calendar
- **System Settings > Privacy & Security > Calendars** — grant your terminal **Full Access** (not just "Add Events Only"), since tindico reads event properties to match and update them

If the app hangs when syncing, check for a buried permission dialog or grant access manually in System Settings.

## How it works

- Fetches upcoming events from your **favorited Indico categories**
- Generates `.ics` files with stable UIDs (re-import updates, not duplicates)
- Uses AppleScript to add Indico URLs to existing Calendar.app entries
