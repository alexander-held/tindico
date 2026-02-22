# tindico

*Swipe right on your meetings.*

A terminal UI for browsing [CERN Indico](https://indico.cern.ch) events and syncing them to macOS Calendar.

## Setup

Create an API token with read permissions: https://indico.cern.ch/user/tokens/.

```bash
# Add your Indico API token to .env
echo "INDICO_API_TOKEN=your_token_here" > .env

# Run tindico via pixi (https://pixi.prefix.dev/latest/installation/)
pixi run tindico
```

## Keybindings

| Key | Action |
|-----|--------|
| `←` | Navigate up into parent category (showing events within ±30 days) |
| `→` | Open event in browser / open material (when in detail panel) / open category |
| `Tab` | Toggle focus between event list and detail panel |
| `/` | Filter events/categories by regex (case-insensitive) |
| `ESC` | Back to favorites view / close window |
| `c` | Export event to Calendar (.ics) |
| `u` | Update existing calendar event with Indico URL |
| `q` | Quit |


## How it works

- Fetches upcoming events from your **favorited Indico categories**
- Generates `.ics` files with stable UIDs (re-import updates, not duplicates)
- Uses EventKit to add Indico URLs to existing Calendar.app entries
