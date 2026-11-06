#!/usr/bin/env python3
"""A single herdr list: every space, with its agents nested underneath.

Run with no arguments inside a plugin pane to draw the list, with --toggle from
the plugin action to open, focus or close it in the current tab, or with --ensure
from the focus hooks to bring it to the tab being focused.
"""
import curses
import fcntl
import glob
import json
import locale
import os
import shlex
import socket
import subprocess
import sys
import time
import unicodedata

PLUGIN_ID = "spaces-list"
ENTRYPOINT = "list"
LABEL = "Spaces"
HELPER_LABELS = (LABEL, "Sidebar")
SHELLS = ("fish", "zsh", "bash", "sh", "dash", "ksh")
MIN_TAB_WIDTH = 100
WIDTH = 32
PAD_X = 1
PAD_Y = 1
FOOTER_LINES = 2
REFRESH_SECONDS = 0.3
IDENTITY_REFRESH_SECONDS = 5.0
TRANSCRIPT_TAIL_BYTES = 1024 * 1024
# Move spaces that need you to the top. Positions then shift as statuses change.
SORT_BY_ATTENTION = True
ATTENTION_ORDER = {"blocked": 0, "done": 1}
# Default terminal titles that say nothing beyond the agent's name.
FILLER_TITLES = {"claude code"}
ENTER_KEYS = (curses.KEY_ENTER, 10, 13)
BACKSPACE_KEYS = (curses.KEY_BACKSPACE, 127, 8)
ESCAPE = 27
CTRL_U = 21
# Keys that keep working normally while typing a filter.
FILTER_PASSTHROUGH = (curses.KEY_UP, curses.KEY_DOWN, curses.KEY_MOUSE, curses.KEY_RESIZE) + ENTER_KEYS

SYMBOLS = {"blocked": "!", "done": "✓", "working": "◐", "idle": "○", "unknown": "·"}
# curses colour pair for each state; pair numbers are assigned in init_colors.
STATE_COLORS = {
    "blocked": curses.COLOR_RED,
    "done": curses.COLOR_GREEN,
    "working": curses.COLOR_YELLOW,
    "idle": curses.COLOR_CYAN,
}
# Claude Code's /color names: (256-colour code, 8-colour fallback).
SESSION_COLORS = {
    "red": (203, curses.COLOR_RED),
    "blue": (75, curses.COLOR_BLUE),
    "green": (114, curses.COLOR_GREEN),
    "yellow": (221, curses.COLOR_YELLOW),
    "purple": (141, curses.COLOR_MAGENTA),
    "orange": (215, curses.COLOR_YELLOW),
    "pink": (211, curses.COLOR_MAGENTA),
    "cyan": (80, curses.COLOR_CYAN),
}


# Colours from herdr's own themes (src/app/state.rs). Other themes keep the
# terminal-palette colours above.
ONE_DARK = {
    "text": "#abb2bf",
    "red": "#e06c75",
    "green": "#98c379",
    "yellow": "#e5c07b",
    "blue": "#61afef",
    "mauve": "#c678dd",
    "teal": "#56b6c2",
    "peach": "#d19a66",
}
PALETTES = {"one-dark": ONE_DARK, "onedark": ONE_DARK}
STATE_TOKENS = {"blocked": "red", "done": "green", "working": "yellow", "idle": "teal"}
SESSION_TOKENS = {
    "red": "red",
    "blue": "blue",
    "green": "green",
    "yellow": "yellow",
    "purple": "mauve",
    "orange": "peach",
    "cyan": "teal",
}


def rpc(method, params=None):
    """One newline-delimited JSON request over the herdr socket."""
    request = {"id": "spaces-list", "method": method, "params": params or {}}
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(2)
        sock.connect(os.environ["HERDR_SOCKET_PATH"])
        sock.sendall((json.dumps(request) + "\n").encode())
        buffer = b""
        while not buffer.endswith(b"\n"):
            chunk = sock.recv(65536)
            if not chunk:
                break
            buffer += chunk
    response = json.loads(buffer)
    if "error" in response:
        raise RuntimeError(response["error"].get("message", "herdr request failed"))
    return response["result"]


def fetch_snapshot():
    result = rpc("session.snapshot")
    return result.get("snapshot", result)


def herdr(*args):
    binary = os.environ.get("HERDR_BIN_PATH", "herdr")
    return subprocess.run(
        [binary, *args], capture_output=True, text=True, timeout=5, check=True
    ).stdout


def parse_identity(text):
    """The latest session name and colour from Claude transcript lines."""
    identity = {"name": None, "color": None}
    for line in text.splitlines():
        if not any(key in line for key in ('"agent-name"', '"custom-title"', '"agent-color"')):
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        kind = entry.get("type")
        if kind == "agent-name":
            identity["name"] = entry.get("agentName")
        elif kind == "custom-title":
            identity["name"] = entry.get("customTitle")
        elif kind == "agent-color":
            identity["color"] = entry.get("agentColor")
    return identity


def user_session_name(session):
    """The session file's name, unless Claude derived it automatically."""
    return session.get("name") if session.get("nameSource") == "user" else None


def read_transcript_identity(path):
    size = os.path.getsize(path)
    with open(path, "rb") as transcript:
        transcript.seek(max(size - TRANSCRIPT_TAIL_BYTES, 0))
        identity = parse_identity(transcript.read().decode("utf-8", "replace"))
        if size > TRANSCRIPT_TAIL_BYTES and not (identity["name"] or identity["color"]):
            transcript.seek(0)
            identity = parse_identity(transcript.read().decode("utf-8", "replace"))
    return identity


def claude_identity(pane_id):
    """Pane -> Claude process -> session file -> transcript name and colour."""
    info = rpc("pane.process_info", {"pane_id": pane_id})["process_info"]
    pid = next(
        (p["pid"] for p in info.get("foreground_processes", []) if p.get("argv0") == "claude"),
        None,
    )
    if pid is None:
        return None
    claude_dir = os.path.expanduser("~/.claude")
    with open(os.path.join(claude_dir, "sessions", f"{pid}.json")) as session_file:
        session = json.load(session_file)
    # No transcript exists until a session's first message.
    pattern = os.path.join(claude_dir, "projects", "*", f"{session['sessionId']}.jsonl")
    matches = glob.glob(pattern)
    identity = read_transcript_identity(matches[0]) if matches else {"name": None, "color": None}
    identity["name"] = user_session_name(session) or identity["name"]
    return identity


def refresh_identities(snap, identities):
    """Update Claude identities in place, keeping the last good value on errors."""
    panes = {a["pane_id"] for a in snap.get("agents", []) if a.get("agent") == "claude"}
    for pane_id in set(identities) - panes:
        del identities[pane_id]
    for pane_id in panes:
        try:
            identity = claude_identity(pane_id)
        except (OSError, RuntimeError, ValueError, KeyError):
            continue
        if identity is not None:
            identities[pane_id] = identity


def agent_text(agent, session_name=None):
    if session_name:
        return session_name
    name = agent.get("label") or agent.get("agent") or "agent"
    title = agent.get("terminal_title_stripped") or ""
    if title in ("", name) or title.lower() in FILLER_TITLES:
        return name
    return f"{name} · {title}"


def attention_sorted(spaces):
    """Blocked spaces first, then done; each group keeps herdr's own order."""
    if not SORT_BY_ATTENTION:
        return list(spaces)
    return sorted(spaces, key=lambda s: ATTENTION_ORDER.get(s.get("agent_status"), 2))


def text_width(text):
    """Terminal columns a string occupies; wide characters take two."""
    width = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        width += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return width


def ellipsize(text, columns):
    """Cut text to fit the given columns, ending in … when anything was lost."""
    if text_width(text) <= columns:
        return text
    if columns <= 0:
        return ""
    kept = ""
    for char in text:
        if text_width(kept + char) > columns - 1:
            break
        kept += char
    return kept.rstrip() + "…"


def agent_search_text(agent, text, tab_label, space_label):
    """Everything a filter can match on for one agent, lowercased."""
    cwd = os.path.basename((agent.get("foreground_cwd") or agent.get("cwd") or "").rstrip("/"))
    fields = (text, agent.get("agent"), agent.get("agent_status"), agent.get("terminal_title_stripped"),
              tab_label, cwd, space_label)
    return " ".join(field for field in fields if field).lower()


def filter_rows(rows, query):
    """Rows matching every word: a matching space keeps all its agents."""
    words = query.lower().split()
    if not words:
        return rows
    matches = lambda row: all(word in row["search"] for word in words)
    kept, space, agents = [], None, []

    def flush():
        if space is None:
            return
        if matches(space):
            kept.extend([space] + agents)
        else:
            hits = [agent for agent in agents if matches(agent)]
            if hits:
                kept.extend([space] + hits)

    for row in rows:
        if row["kind"] == "space":
            flush()
            space, agents = row, []
        else:
            agents.append(row)
    flush()
    return kept


def first_match(rows):
    """Where the cursor lands after a filter change: the first agent, if any."""
    return next((i for i, row in enumerate(rows) if row["kind"] == "agent"), 0)


def edit_query(query, key):
    """The filter text after a key press; None means leave filter mode."""
    if key == ESCAPE:
        return None
    if key in BACKSPACE_KEYS:
        return query[:-1]
    if key == CTRL_U:
        return ""
    if 32 <= key < 127:
        return query + chr(key)
    return query


def build_rows(snap, identities=None):
    """Flatten a session snapshot into space rows followed by their agents."""
    identities = identities or {}
    tab_numbers = {t["tab_id"]: t.get("number", 0) for t in snap.get("tabs", [])}
    tab_labels = {t["tab_id"]: t.get("label", "") for t in snap.get("tabs", [])}
    focused_space = snap.get("focused_workspace_id")
    focused_pane = snap.get("focused_pane_id")
    rows = []
    for space in attention_sorted(snap.get("workspaces", [])):
        space_id = space["workspace_id"]
        rows.append({
            "kind": "space",
            "target": space_id,
            "tab": space.get("active_tab_id"),
            "state": space.get("agent_status", "unknown"),
            "text": space.get("label") or space_id,
            "detail": "",
            "search": (space.get("label") or space_id).lower(),
            "current": space_id == focused_space,
        })
        agents = [a for a in snap.get("agents", []) if a.get("workspace_id") == space_id]
        agents.sort(key=lambda a: tab_numbers.get(a.get("tab_id"), 0))
        several_tabs = space.get("tab_count", 1) > 1
        for agent in agents:
            number = tab_numbers.get(agent.get("tab_id"), 0) if several_tabs else 0
            identity = identities.get(agent["pane_id"], {})
            text = agent_text(agent, identity.get("name"))
            rows.append({
                "kind": "agent",
                "target": agent["pane_id"],
                "tab": agent.get("tab_id"),
                "state": agent.get("agent_status", "unknown"),
                "text": text,
                "search": agent_search_text(
                    agent, text, tab_labels.get(agent.get("tab_id")), space.get("label")
                ),
                "detail": f"t{number}" if number else "",
                "color": identity.get("color"),
                "current": agent["pane_id"] == focused_pane,
            })
    return rows


def followed_key(snap, own_pane, rows):
    """The row matching herdr's focus, or None while the list itself has focus."""
    focused = snap.get("focused_pane_id")
    if focused == own_pane:
        return None
    if ("agent", focused) in [(r["kind"], r["target"]) for r in rows]:
        return ("agent", focused)
    return ("space", snap.get("focused_workspace_id"))


def opening_key(snap, own_pane, own_tab):
    """Where a newly opened list starts: this tab's agent, else its space."""
    for agent in snap.get("agents", []):
        if agent.get("tab_id") == own_tab:
            return ("agent", agent["pane_id"])
    for pane in snap.get("panes", []):
        if pane.get("pane_id") == own_pane:
            return ("space", pane.get("workspace_id"))
    return None


def locate_self(snap, pane_id, tab_id, terminal_id):
    """Where this list is now: a move can change its pane id, never its terminal."""
    for pane in snap.get("panes", []):
        found = pane.get("terminal_id") == terminal_id if terminal_id else pane.get("pane_id") == pane_id
        if found:
            return pane["pane_id"], pane.get("tab_id"), pane.get("terminal_id")
    return pane_id, tab_id, terminal_id


def rightmost_pane(snap, tab_id):
    """The pane to split when docking: the right column, topmost first."""
    for layout in snap.get("layouts", []):
        if layout.get("tab_id") == tab_id and layout.get("panes"):
            return min(layout["panes"], key=lambda p: (-(p["rect"]["x"] + p["rect"]["width"]), p["rect"]["y"]))
    return None


def list_pane_in_tab(snap, tab_id):
    for pane in snap.get("panes", []):
        if pane.get("tab_id") == tab_id and pane.get("label") == LABEL:
            return pane["pane_id"]
    return None


def list_process_state(processes):
    """What a list pane is doing: "running", a bare "shell", or something "other"."""
    if any("spaces_list.py" in (p.get("cmdline") or "") for p in processes):
        return "running"
    names = [os.path.basename(p.get("argv0") or "").lstrip("-") for p in processes]
    return "shell" if names and all(name in SHELLS for name in names) else "other"


def revive(pane_id, attempts=20):
    """Herdr restores a list pane as a plain shell: start the list in it again.

    This keeps the pane's place and width, and leaves anything else running there
    alone. Returns True when the pane is now running the list or was told to.
    """
    for _ in range(attempts):
        info = rpc("pane.process_info", {"pane_id": pane_id})["process_info"]
        state = list_process_state(info.get("foreground_processes", []))
        if state == "running":
            return True
        if state == "shell":
            command = f"exec {shlex.quote(sys.executable)} {shlex.quote(os.path.abspath(__file__))}"
            herdr("pane", "send-text", pane_id, command + "\n")
            return True
        if info.get("foreground_processes"):
            return False
        time.sleep(0.25)
    return False


def revive_all():
    """Startup hook: bring back every list that a server restart turned into a shell."""
    for pane in fetch_snapshot().get("panes", []):
        if pane.get("label") == LABEL:
            revive(pane["pane_id"])


def dock(snap, tab_id):
    """Open the list at the right edge of a tab and size it to WIDTH columns.

    Opening moves focus to the new pane, so callers that want focus elsewhere
    must set it again afterwards.
    """
    target = rightmost_pane(snap, tab_id)
    if target is None:
        return None
    opened = json.loads(herdr(
        "plugin", "pane", "open",
        "--plugin", PLUGIN_ID,
        "--entrypoint", ENTRYPOINT,
        "--placement", "split",
        "--target-pane", target["pane_id"],
        "--direction", "right",
    ))
    pane_id = opened["result"]["plugin_pane"]["pane"]["pane_id"]
    share = WIDTH / max(target["rect"]["width"], 1)
    herdr("pane", "resize", "--pane", pane_id, "--direction", "right",
          "--amount", f"{max(0.5 - share, 0.0):.4f}")
    correct_width(tab_id, pane_id)
    return pane_id


def correct_width(tab_id, pane_id):
    """Another plugin may have split the tab meanwhile, skewing the first resize."""
    for layout in fetch_snapshot().get("layouts", []):
        if layout.get("tab_id") != tab_id:
            continue
        width = next((p["rect"]["width"] for p in layout["panes"] if p["pane_id"] == pane_id), WIDTH)
        off_by = width - WIDTH
        if abs(off_by) > 2:
            herdr("pane", "resize", "--pane", pane_id,
                  "--direction", "right" if off_by > 0 else "left",
                  "--amount", f"{abs(off_by) / layout['area']['width']:.4f}")


def state_dir():
    path = os.environ.get("HERDR_PLUGIN_STATE_DIR") or os.path.expanduser(
        f"~/.local/state/herdr/plugins/{PLUGIN_ID}"
    )
    os.makedirs(path, exist_ok=True)
    return path


def marker_path(tab_id):
    """Marks a tab whose list the user closed, so the hooks leave it closed."""
    return os.path.join(state_dir(), f"closed-{tab_id.replace(':', '_')}")


def mark_closed(tab_id):
    if tab_id:
        with open(marker_path(tab_id), "w"):
            pass


def clear_closed(tab_id):
    try:
        os.remove(marker_path(tab_id))
    except FileNotFoundError:
        pass


def sweep_closed_markers(snap):
    """Tab ids get recycled, so forget markers for tabs that no longer exist."""
    live = {marker_path(t["tab_id"]) for t in snap.get("tabs", [])}
    for path in glob.glob(os.path.join(state_dir(), "closed-*")):
        if path not in live:
            os.remove(path)


def dock_once(tab_id):
    """Open a list unless a focus hook just did; both take the same lock."""
    with open(os.path.join(state_dir(), "dock.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        snap = fetch_snapshot()
        return list_pane_in_tab(snap, tab_id) or dock(snap, tab_id)


def toggle():
    snap = fetch_snapshot()
    tab_id = snap.get("focused_tab_id")
    pane_id = list_pane_in_tab(snap, tab_id)
    if pane_id is None:
        clear_closed(tab_id)
        dock_once(tab_id)
    elif revive(pane_id) and pane_id == snap.get("focused_pane_id"):
        mark_closed(tab_id)
        herdr("pane", "close", pane_id)
    else:
        rpc("pane.focus", {"pane_id": pane_id})


def find():
    """Open or focus the list in this tab and start filtering straight away."""
    snap = fetch_snapshot()
    tab_id = snap.get("focused_tab_id")
    pane_id = list_pane_in_tab(snap, tab_id)
    if pane_id is None:
        clear_closed(tab_id)
        pane_id = dock_once(tab_id)
    else:
        revive(pane_id)
        rpc("pane.focus", {"pane_id": pane_id})
    if pane_id:
        herdr("pane", "send-text", pane_id, "/")


def event_tab(event, snap):
    """The tab a focus event is moving to; global focus may still be the old one."""
    data = event.get("data", event)
    tab_id = data.get("tab_id") or (data.get("tab") or {}).get("tab_id")
    if tab_id:
        return tab_id
    workspace_id = data.get("workspace_id") or (data.get("workspace") or {}).get("workspace_id")
    for space in snap.get("workspaces", []):
        if space["workspace_id"] == workspace_id:
            return space.get("active_tab_id")
    return snap.get("focused_tab_id")


def has_room(snap, tab_id):
    """False for missing, zoomed or narrow tabs, where a split would get in the way."""
    for layout in snap.get("layouts", []):
        if layout.get("tab_id") == tab_id:
            return (
                not layout.get("zoomed")
                and layout.get("area", {}).get("width", 0) >= MIN_TAB_WIDTH
            )
    return False


def only_helpers_left(snap, tab_id):
    """True once a tab holds nothing but plugin panes like this list."""
    panes = [p for p in snap.get("panes", []) if p.get("tab_id") == tab_id]
    return bool(panes) and all(p.get("label") in HELPER_LABELS for p in panes)


def stale_list_panes(snap, tab_id):
    """Lists outside the active tab, unless closing one would close its whole tab."""
    return [
        pane["pane_id"]
        for pane in snap.get("panes", [])
        if pane.get("label") == LABEL
        and pane.get("tab_id") != tab_id
        and not only_helpers_left(snap, pane.get("tab_id"))
    ]


def target_path():
    """The tab this session's focus hooks last moved to; the state dir is shared."""
    session = os.environ.get("HERDR_SOCKET_PATH", "").replace(os.sep, "_")
    return os.path.join(state_dir(), f"target-{session}")


def remember_target(tab_id):
    path = target_path()
    temporary = f"{path}.{os.getpid()}"
    with open(temporary, "w") as target:
        target.write(tab_id)
    os.replace(temporary, path)


def latest_target(fallback):
    try:
        with open(target_path()) as target:
            return target.read().strip() or fallback
    except FileNotFoundError:
        return fallback


def close_lists(pane_ids):
    closed = False
    for pane_id in pane_ids:
        try:
            herdr("pane", "close", pane_id)
            closed = True
        except subprocess.SubprocessError:
            pass  # Already gone; the next tab switch sweeps anything left.
    return closed


def move_list(snap, pane_id, tab_id):
    """Bring a running list to a tab's right edge; returns its id, which may change.

    The split starts at the final width, so the tab's panes settle in one resize
    and the list keeps its process and what it has drawn.
    """
    target = rightmost_pane(snap, tab_id)
    if target is None:
        return None
    share = max(1 - WIDTH / max(target["rect"]["width"], 1), 0.5)
    moved = json.loads(herdr(
        "pane", "move", pane_id,
        "--tab", tab_id,
        "--split", "right",
        "--target-pane", target["pane_id"],
        "--ratio", f"{share:.4f}",
        "--no-focus",
    ))
    new_id = moved["result"]["move_result"]["pane"]["pane_id"]
    # A move changes the layout without telling the panes, so an agent would keep
    # drawing at its old width. Any resize makes herdr deliver the new size.
    herdr("pane", "resize", "--pane", new_id, "--direction", "right", "--amount", "0.0001")
    correct_width(tab_id, new_id)
    return new_id


def place_list(snap, spare, tab_id):
    """Show the list in a tab, by moving a spare one if there is one."""
    if spare is None:
        return dock(snap, tab_id)
    try:
        return move_list(snap, spare, tab_id)
    except (subprocess.SubprocessError, ValueError, KeyError):
        # The move may have half worked, so look again before opening another.
        snap = fetch_snapshot()
        close_lists(stale_list_panes(snap, tab_id))
        return list_pane_in_tab(snap, tab_id) or dock(snap, tab_id)


def sync_list(tab_id):
    """Keep one list: bring it to the tab being focused and close any others."""
    snap = fetch_snapshot()
    stale = stale_list_panes(snap, tab_id)
    marked = os.path.exists(marker_path(tab_id))
    existing = list_pane_in_tab(snap, tab_id)
    wanted = (
        not marked
        and not existing
        and has_room(snap, tab_id)
        and not only_helpers_left(snap, tab_id)
    )
    spare = stale.pop(0) if wanted and stale else None
    if close_lists(stale):
        snap = fetch_snapshot()
    if existing and not marked:
        revive(existing)
    if not wanted:
        return
    previous = snap.get("focused_pane_id")
    target = rightmost_pane(snap, tab_id)
    pane_id = place_list(snap, spare, tab_id)
    in_tab = previous in {p["pane_id"] for p in snap["panes"] if p.get("tab_id") == tab_id}
    if pane_id and fetch_snapshot().get("focused_pane_id") == pane_id:
        rpc("pane.focus", {"pane_id": previous if in_tab else target["pane_id"]})


def ensure():
    """Focus hook: move the single list to the tab being focused, keeping focus put."""
    snap = fetch_snapshot()
    sweep_closed_markers(snap)
    event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    tab_id = event_tab(event, snap)
    if not tab_id:
        return
    # Record the move before queueing, so quick switches settle on the last tab
    # rather than whichever hook happened to win the lock.
    remember_target(tab_id)
    with open(os.path.join(state_dir(), "dock.lock"), "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sync_list(latest_target(tab_id))


def activate(row):
    """Jump to a row; the tab.focused hook opens a list there if it needs one."""
    if row["kind"] == "agent":
        herdr("agent", "focus", row["target"])
    else:
        herdr("workspace", "focus", row["target"])


def config_path():
    base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
    return os.path.join(base, "herdr", "config.toml")


def configured_palette(text):
    """The palette for the theme named in herdr's config text, if we have one."""
    try:
        import tomllib
        name = tomllib.loads(text).get("theme", {}).get("name")
    except (ImportError, ValueError, AttributeError):
        return None
    return PALETTES.get(name)


def load_palette():
    try:
        with open(config_path(), encoding="utf-8") as config:
            return configured_palette(config.read())
    except OSError:
        return None


def nearest_256(hex_color):
    """The closest xterm-256 colour, from its colour cube and grey ramp."""
    red, green, blue = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    levels = [0, 95, 135, 175, 215, 255]
    candidates = {
        16 + 36 * r + 6 * g + b: (levels[r], levels[g], levels[b])
        for r in range(6) for g in range(6) for b in range(6)
    }
    candidates.update({232 + i: (8 + 10 * i,) * 3 for i in range(24)})
    return min(
        candidates,
        key=lambda code: sum((a - b) ** 2 for a, b in zip((red, green, blue), candidates[code])),
    )


def source_mtime():
    """Change times of this script and herdr's config; None if the script is missing."""
    try:
        script = os.path.getmtime(os.path.abspath(__file__))
    except OSError:
        return None
    try:
        return (script, os.path.getmtime(config_path()))
    except OSError:
        return (script, None)


def source_changed(started_with, current):
    """True once the script or config changes; a missing script is mid-save, so wait."""
    return current is not None and current != started_with


def init_colors(palette=None):
    pairs = {}
    if not curses.has_colors():
        return pairs
    curses.start_color()
    curses.use_default_colors()
    wide = curses.COLORS >= 256
    themed = palette if wide else None
    if themed:
        text_pair = len(STATE_COLORS) + len(SESSION_COLORS) + 1
        curses.init_pair(text_pair, nearest_256(themed["text"]), -1)
        pairs["text"] = curses.color_pair(text_pair)
    for number, (state, color) in enumerate(STATE_COLORS.items(), start=1):
        code = nearest_256(themed[STATE_TOKENS[state]]) if themed else color
        curses.init_pair(number, code, -1)
        pairs[state] = curses.color_pair(number)
    for number, (name, codes) in enumerate(SESSION_COLORS.items(), start=len(STATE_COLORS) + 1):
        token = SESSION_TOKENS.get(name)
        if themed and token:
            code = nearest_256(themed[token])
        else:
            code = codes[0] if wide else codes[1]
        curses.init_pair(number, code, -1)
        pairs[name] = curses.color_pair(number)
    return pairs


def put(window, y, x, text, attr=0):
    height, width = window.getmaxyx()
    if 0 <= y < height and x < width:
        try:
            window.addnstr(y, x, text, width - x - 1, attr)
        except curses.error:
            pass


def display_lines(rows):
    """Screen lines as row indexes, with None for the gap between spaces."""
    lines = []
    for index, row in enumerate(rows):
        if row["kind"] == "space" and lines:
            lines.append(None)
        lines.append(index)
    return lines


def scroll_top(lines, cursor, top, body):
    """The first visible line that keeps the cursor's row on screen."""
    position = lines.index(cursor) if cursor in lines else 0
    if position < top:
        top = position
    elif position >= top + body:
        top = position - body + 1
    return max(0, min(top, max(len(lines) - body, 0)))


def row_at(lines, top, y):
    """The row drawn at screen line y, or None for gaps and empty space."""
    position = top + y - PAD_Y
    if y < PAD_Y or not 0 <= position < len(lines):
        return None
    return lines[position]


def body_height(height):
    return max(height - PAD_Y - FOOTER_LINES, 1)


def row_styles(row, colors):
    """(symbol attr, text attr) for a row, with blocked rows taking over both."""
    blocked = curses.A_BOLD | colors.get("blocked", 0)
    if row["state"] == "blocked":
        return blocked, blocked
    state_attr = colors.get(row["state"], curses.A_DIM)
    if row["kind"] == "space":
        return state_attr | curses.A_DIM, curses.A_BOLD
    text_attr = curses.A_BOLD if row["current"] else 0
    return state_attr, text_attr | colors.get(row.get("color"), 0)


def draw_row(window, y, row, last_in_space, colors):
    width = window.getmaxyx()[1]
    right_edge = width - 1 - PAD_X
    symbol = SYMBOLS.get(row["state"], "·")
    symbol_attr, text_attr = row_styles(row, colors)
    if row["kind"] == "space":
        symbol_x = PAD_X
    else:
        put(window, y, PAD_X, f" {'└' if last_in_space else '├'} ", curses.A_DIM)
        symbol_x = PAD_X + 3
    put(window, y, symbol_x, symbol, symbol_attr)
    text_x = symbol_x + 2
    detail = row.get("detail", "")
    room = right_edge - text_x - (len(detail) + 1 if detail else 0)
    put(window, y, text_x, ellipsize(row["text"], room), text_attr)
    if detail:
        put(window, y, right_edge - len(detail), detail, curses.A_DIM)


def draw(window, rows, cursor, top, colors, notice, query=None):
    window.erase()
    height, width = window.getmaxyx()
    lines = display_lines(rows)
    last_index = {}
    for index, row in enumerate(rows):
        if row["kind"] == "agent":
            last_index[row["space_index"]] = index
    for offset, index in enumerate(lines[top:top + body_height(height)]):
        if index is None:
            continue
        y = PAD_Y + offset
        row = rows[index]
        draw_row(window, y, row, last_index.get(row["space_index"]) == index, colors)
        if index == cursor:
            # One solid bar: restyle the whole line rather than each piece.
            try:
                window.chgat(y, 0, width, curses.A_REVERSE | curses.A_BOLD | colors.get("text", 0))
            except curses.error:
                pass
    if query is not None:
        if not rows:
            put(window, PAD_Y, PAD_X, "no matches", curses.A_DIM)
        # Keep the end of a long query visible, since that's where you're typing.
        visible = query[-max(width - PAD_X - 4, 1):]
        put(window, height - 1, PAD_X, f"/{visible}█", curses.A_BOLD)
    else:
        footer = notice or "↑↓ move · / find · ⏎ jump"
        put(window, height - 1, PAD_X, footer, curses.A_DIM)
    window.refresh()


def index_rows(rows):
    """Record which space each agent belongs to, for drawing tree branches."""
    space_index = -1
    for index, row in enumerate(rows):
        if row["kind"] == "space":
            space_index = index
        row["space_index"] = space_index
    return rows


def run(window):
    locale.setlocale(locale.LC_ALL, "")
    curses.curs_set(0)
    curses.mousemask(curses.ALL_MOUSE_EVENTS)
    curses.mouseinterval(0)
    window.timeout(100)
    # Esc must clear a filter at once, not after curses' default one second wait.
    curses.set_escdelay(25)
    colors = init_colors(load_palette())
    window.bkgd(" ", colors.get("text", 0))
    all_rows, rows, cursor, top, notice = [], [], 0, 0, ""
    query = None
    selected_key = None
    next_refresh = 0.0
    identities, next_identities = {}, 0.0
    own_pane = os.environ.get("HERDR_PANE_ID")
    own_tab = os.environ.get("HERDR_TAB_ID")
    own_terminal = None
    started_with = source_mtime()
    while True:
        if time.monotonic() >= next_refresh:
            if source_changed(started_with, source_mtime()):
                # Open lists would otherwise run old code until closed by hand.
                curses.endwin()
                os.execv(sys.executable, [sys.executable] + sys.argv)
            try:
                snap = fetch_snapshot()
                own_pane, own_tab, own_terminal = locate_self(snap, own_pane, own_tab, own_terminal)
                # The reload above re-runs this file with this environment.
                if own_pane and own_tab:
                    os.environ.update(HERDR_PANE_ID=own_pane, HERDR_TAB_ID=own_tab)
                if only_helpers_left(snap, own_tab):
                    return
                if time.monotonic() >= next_identities:
                    refresh_identities(snap, identities)
                    next_identities = time.monotonic() + IDENTITY_REFRESH_SECONDS
                all_rows = build_rows(snap, identities)
                followed = followed_key(snap, own_pane, all_rows)
                if followed:
                    # Focus moved away, so a half-typed filter no longer applies.
                    query = None
                    selected_key = followed
                elif selected_key is None:
                    selected_key = opening_key(snap, own_pane, own_tab)
                rows = index_rows(filter_rows(all_rows, query or ""))
                notice = ""
            except (OSError, RuntimeError, ValueError) as error:
                notice = f"error: {error}"
            next_refresh = time.monotonic() + REFRESH_SECONDS
            # Keep the highlight on the same row as the list changes around it.
            keys = [(r["kind"], r["target"]) for r in rows]
            if selected_key in keys:
                cursor = keys.index(selected_key)
        cursor = max(0, min(cursor, len(rows) - 1))
        lines = display_lines(rows)
        top = scroll_top(lines, cursor, top, body_height(window.getmaxyx()[0]))
        draw(window, rows, cursor, top, colors, notice, query)

        key = window.getch()
        target_row = None
        if query is not None and key != -1 and key not in FILTER_PASSTHROUGH:
            before = query
            query = edit_query(query, key)
            if query != before:
                rows = index_rows(filter_rows(all_rows, query or ""))
                cursor = first_match(rows)
            key = -1
        if key in (ord("q"), ESCAPE):
            mark_closed(own_tab)
            return
        if key == ord("/"):
            query = ""
        elif key in (ord("j"), curses.KEY_DOWN):
            cursor += 1
        elif key in (ord("k"), curses.KEY_UP):
            cursor -= 1
        elif key in ENTER_KEYS:
            target_row = cursor
        elif key == curses.KEY_RESIZE:
            next_refresh = 0.0
        elif key == curses.KEY_MOUSE:
            try:
                _, _, y, _, state = curses.getmouse()
            except curses.error:
                continue
            clicked = row_at(lines, top, y)
            if state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED) and clicked is not None:
                cursor = clicked
                target_row = clicked
        if rows:
            cursor = max(0, min(cursor, len(rows) - 1))
            selected_key = (rows[cursor]["kind"], rows[cursor]["target"])
        if target_row is not None and target_row < len(rows):
            try:
                activate(rows[target_row])
                query = None
                next_refresh = 0.0
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                notice = f"error: {error}"


def main():
    if "--toggle" in sys.argv[1:]:
        toggle()
        return
    if "--find" in sys.argv[1:]:
        find()
        return
    if "--startup" in sys.argv[1:]:
        revive_all()
        return
    if "--ensure" in sys.argv[1:]:
        ensure()
        return
    curses.wrapper(run)


if __name__ == "__main__":
    main()
