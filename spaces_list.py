#!/usr/bin/env python3
"""A single herdr list: every space, with its agents nested underneath.

Run with no arguments inside a plugin pane to draw the list, with --toggle from
the plugin action to open, focus or close it in the current tab, or with --ensure
from the focus hooks to open it in a tab that does not have one yet.
"""
import curses
import fcntl
import glob
import json
import locale
import os
import socket
import subprocess
import sys
import time

PLUGIN_ID = "spaces-list"
ENTRYPOINT = "list"
LABEL = "Spaces"
HELPER_LABELS = (LABEL, "Sidebar")
MIN_TAB_WIDTH = 100
WIDTH = 32
REFRESH_SECONDS = 0.3
IDENTITY_REFRESH_SECONDS = 5.0
TRANSCRIPT_TAIL_BYTES = 1024 * 1024

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


def agent_text(agent, tab_number, session_name=None):
    name = agent.get("label") or agent.get("agent") or "agent"
    title = agent.get("terminal_title_stripped") or ""
    text = name if title in ("", name) else f"{name} · {title}"
    text = session_name or text
    return f"t{tab_number} {text}" if tab_number else text


def build_rows(snap, identities=None):
    """Flatten a session snapshot into space rows followed by their agents."""
    identities = identities or {}
    tab_numbers = {t["tab_id"]: t.get("number", 0) for t in snap.get("tabs", [])}
    focused_space = snap.get("focused_workspace_id")
    focused_pane = snap.get("focused_pane_id")
    rows = []
    for space in snap.get("workspaces", []):
        space_id = space["workspace_id"]
        rows.append({
            "kind": "space",
            "target": space_id,
            "tab": space.get("active_tab_id"),
            "state": space.get("agent_status", "unknown"),
            "text": space.get("label") or space_id,
            "current": space_id == focused_space,
        })
        agents = [a for a in snap.get("agents", []) if a.get("workspace_id") == space_id]
        agents.sort(key=lambda a: tab_numbers.get(a.get("tab_id"), 0))
        several_tabs = space.get("tab_count", 1) > 1
        for agent in agents:
            number = tab_numbers.get(agent.get("tab_id"), 0) if several_tabs else 0
            identity = identities.get(agent["pane_id"], {})
            rows.append({
                "kind": "agent",
                "target": agent["pane_id"],
                "tab": agent.get("tab_id"),
                "state": agent.get("agent_status", "unknown"),
                "text": agent_text(agent, number, identity.get("name")),
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


def leftmost_pane(snap, tab_id):
    """The pane to split when docking: the left column, topmost first."""
    for layout in snap.get("layouts", []):
        if layout.get("tab_id") == tab_id and layout.get("panes"):
            return min(layout["panes"], key=lambda p: (p["rect"]["x"], p["rect"]["y"]))
    return None


def list_pane_in_tab(snap, tab_id):
    for pane in snap.get("panes", []):
        if pane.get("tab_id") == tab_id and pane.get("label") == LABEL:
            return pane["pane_id"]
    return None


def dock(snap, tab_id):
    """Open the list at the left edge of a tab and size it to WIDTH columns.

    The swap below moves focus to the new pane, so callers that want focus
    elsewhere must set it again afterwards.
    """
    target = leftmost_pane(snap, tab_id)
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
    # Herdr only splits rightwards here, so swap to reach the left edge.
    herdr("pane", "swap", "--source-pane", pane_id, "--target-pane", target["pane_id"])
    share = WIDTH / max(target["rect"]["width"], 1)
    herdr("pane", "resize", "--pane", pane_id, "--direction", "left",
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
                  "--direction", "left" if off_by > 0 else "right",
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


def toggle():
    snap = fetch_snapshot()
    tab_id = snap.get("focused_tab_id")
    pane_id = list_pane_in_tab(snap, tab_id)
    if pane_id is None:
        clear_closed(tab_id)
        dock(snap, tab_id)
    elif pane_id == snap.get("focused_pane_id"):
        mark_closed(tab_id)
        herdr("pane", "close", pane_id)
    else:
        herdr("plugin", "pane", "focus", pane_id)


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


def ensure():
    """Focus hook: open the list in the tab being focused, keeping focus put."""
    snap = fetch_snapshot()
    sweep_closed_markers(snap)
    event = json.loads(os.environ.get("HERDR_PLUGIN_EVENT_JSON") or "{}")
    tab_id = event_tab(event, snap)
    if not tab_id or os.path.exists(marker_path(tab_id)):
        return
    with open(os.path.join(state_dir(), "dock.lock"), "w") as lock:
        # tab.focused and workspace.focused often fire together; one is enough.
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            return
        snap = fetch_snapshot()
        if list_pane_in_tab(snap, tab_id) or not has_room(snap, tab_id):
            return
        if only_helpers_left(snap, tab_id):
            return
        previous = snap.get("focused_pane_id")
        target = leftmost_pane(snap, tab_id)
        pane_id = dock(snap, tab_id)
        in_tab = previous in {p["pane_id"] for p in snap["panes"] if p.get("tab_id") == tab_id}
        if pane_id and fetch_snapshot().get("focused_pane_id") == pane_id:
            rpc("pane.focus", {"pane_id": previous if in_tab else target["pane_id"]})


def activate(row):
    """Jump to a row; the tab.focused hook opens a list there if it needs one."""
    if row["kind"] == "agent":
        herdr("agent", "focus", row["target"])
    else:
        herdr("workspace", "focus", row["target"])


def init_colors():
    pairs = {}
    if not curses.has_colors():
        return pairs
    curses.start_color()
    curses.use_default_colors()
    for number, (state, color) in enumerate(STATE_COLORS.items(), start=1):
        curses.init_pair(number, color, -1)
        pairs[state] = curses.color_pair(number)
    wide = curses.COLORS >= 256
    for number, (name, codes) in enumerate(SESSION_COLORS.items(), start=len(STATE_COLORS) + 1):
        curses.init_pair(number, codes[0] if wide else codes[1], -1)
        pairs[name] = curses.color_pair(number)
    return pairs


def put(window, y, x, text, attr=0):
    height, width = window.getmaxyx()
    if 0 <= y < height and x < width:
        try:
            window.addnstr(y, x, text, width - x - 1, attr)
        except curses.error:
            pass


def draw(window, rows, cursor, top, colors, notice):
    window.erase()
    height, _ = window.getmaxyx()
    body = max(height - 1, 1)
    last_index = {}
    for index, row in enumerate(rows):
        if row["kind"] == "agent":
            last_index[row["space_index"]] = index
    for y, index in enumerate(range(top, min(top + body, len(rows)))):
        row = rows[index]
        symbol = SYMBOLS.get(row["state"], "·")
        state_attr = colors.get(row["state"], curses.A_DIM)
        selected = curses.A_REVERSE if index == cursor else 0
        if row["kind"] == "space":
            put(window, y, 0, symbol, state_attr | selected)
            label = f" {row['text']}"
            attr = curses.A_BOLD if row["current"] else 0
            put(window, y, 1, label, attr | selected)
        else:
            branch = "└" if last_index.get(row["space_index"]) == index else "├"
            put(window, y, 0, f" {branch} ", curses.A_DIM | selected)
            put(window, y, 3, symbol, state_attr | selected)
            attr = curses.A_BOLD if row["current"] else 0
            attr |= colors.get(row.get("color"), 0)
            put(window, y, 4, f" {row['text']}", attr | selected)
    footer = notice or "↑↓/jk move · ⏎ jump · q close"
    put(window, height - 1, 0, footer, curses.A_DIM)
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
    colors = init_colors()
    rows, cursor, top, notice = [], 0, 0, ""
    selected_key = None
    next_refresh = 0.0
    identities, next_identities = {}, 0.0
    own_pane = os.environ.get("HERDR_PANE_ID")
    own_tab = os.environ.get("HERDR_TAB_ID")
    while True:
        if time.monotonic() >= next_refresh:
            try:
                snap = fetch_snapshot()
                if only_helpers_left(snap, own_tab):
                    return
                if time.monotonic() >= next_identities:
                    refresh_identities(snap, identities)
                    next_identities = time.monotonic() + IDENTITY_REFRESH_SECONDS
                rows = index_rows(build_rows(snap, identities))
                followed = followed_key(snap, own_pane, rows)
                if followed:
                    selected_key = followed
                elif selected_key is None:
                    selected_key = opening_key(snap, own_pane, own_tab)
                notice = ""
            except (OSError, RuntimeError, ValueError) as error:
                notice = f"error: {error}"
            next_refresh = time.monotonic() + REFRESH_SECONDS
            # Keep the highlight on the same row as the list changes around it.
            keys = [(r["kind"], r["target"]) for r in rows]
            if selected_key in keys:
                cursor = keys.index(selected_key)
        cursor = max(0, min(cursor, len(rows) - 1))
        height = window.getmaxyx()[0]
        body = max(height - 1, 1)
        if cursor < top:
            top = cursor
        elif cursor >= top + body:
            top = cursor - body + 1
        draw(window, rows, cursor, top, colors, notice)

        key = window.getch()
        target_row = None
        if key in (ord("q"), 27):
            mark_closed(own_tab)
            return
        if key in (ord("j"), curses.KEY_DOWN):
            cursor += 1
        elif key in (ord("k"), curses.KEY_UP):
            cursor -= 1
        elif key in (curses.KEY_ENTER, 10, 13):
            target_row = cursor
        elif key == curses.KEY_RESIZE:
            next_refresh = 0.0
        elif key == curses.KEY_MOUSE:
            try:
                _, _, y, _, state = curses.getmouse()
            except curses.error:
                continue
            clicked = top + y
            if state & (curses.BUTTON1_CLICKED | curses.BUTTON1_PRESSED) and clicked < len(rows):
                cursor = clicked
                target_row = clicked
        if rows:
            cursor = max(0, min(cursor, len(rows) - 1))
            selected_key = (rows[cursor]["kind"], rows[cursor]["target"])
        if target_row is not None and target_row < len(rows):
            try:
                activate(rows[target_row])
                next_refresh = 0.0
            except (OSError, RuntimeError, ValueError, subprocess.SubprocessError) as error:
                notice = f"error: {error}"


def main():
    if "--toggle" in sys.argv[1:]:
        toggle()
        return
    if "--ensure" in sys.argv[1:]:
        ensure()
        return
    curses.wrapper(run)


if __name__ == "__main__":
    main()
