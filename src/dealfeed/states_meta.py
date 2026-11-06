"""State names for the registries we load (kept dependency-free so any module can import it)."""

STATE_NAMES = {
    "IA": "Iowa", "CO": "Colorado", "NY": "New York", "CT": "Connecticut", "OR": "Oregon", "TX": "Texas",
}
SUPPORTED_STATES = tuple(STATE_NAMES)


def covered_states_text() -> str:
    names = list(STATE_NAMES.values())
    return ", ".join(names[:-1]) + " and " + names[-1]
