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
| `→` | Open event in browser |
| `ESC` | Back to favorites view |
| `c` | Sync event to Calendar (.ics) |
| `u` | Update existing calendar event with Indico URL |
| `q` | Quit |

Double-click a row to open in browser.

## How it works

- Fetches upcoming events from your **favorited Indico categories**
- Generates `.ics` files with stable UIDs (re-import updates, not duplicates)
- Uses AppleScript to add Indico URLs to existing Calendar.app entries
