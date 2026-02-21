from pathlib import Path


def load_env(path: Path | None = None) -> dict[str, str]:
    """Parse a .env file into a dict of key=value pairs."""
    if path is None:
        path = Path(__file__).resolve().parents[2] / ".env"
    env = {}
    if not path.exists():
        return env
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, _, value = line.partition("=")
            env[key.strip()] = value.strip()
    return env


_env = load_env()

INDICO_BASE_URL: str = _env.get("INDICO_BASE_URL", "https://indico.cern.ch")
INDICO_API_TOKEN: str = _env.get("INDICO_API_TOKEN", "")

if not INDICO_API_TOKEN:
    import sys

    print(
        "Error: INDICO_API_TOKEN not set.\n"
        "Create a .env file in the project root with:\n"
        "  INDICO_API_TOKEN=your_token_here\n"
        "Get a token at https://indico.cern.ch/user/tokens/",
        file=sys.stderr,
    )
    sys.exit(1)
