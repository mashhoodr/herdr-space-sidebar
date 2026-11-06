# herdr-space-sidebar

A [herdr](https://github.com/herdrdev/herdr) plugin that merges the Spaces and Agents panels into one list, with each agent nested under its space.

<img src="docs/screenshot.png" alt="The plugin's list: spaces with their agents nested underneath, showing status symbols and session colors" width="340">

*The plugin's real output, shown with example data.*

Status symbols: `!` blocked, `✓` done, `◐` working, `○` idle, `·` nothing detected.

## Install

Requires herdr 0.9+ and `python3` on Linux or macOS. There is nothing to build.

```bash
herdr plugin install mashhoodr/herdr-space-sidebar
```

Bind a key in `~/.config/herdr/config.toml`, then run `herdr server reload-config`:

```toml
[[keys.command]]
key = "prefix+a"
type = "plugin_action"
command = "spaces-list.toggle"
description = "toggle spaces list"

[[keys.command]]
key = "prefix+/"
type = "plugin_action"
command = "spaces-list.find"
description = "find in spaces list"
```

To hide herdr's own sidebar, add the settings below and press `prefix+b`:

```toml
[ui]
sidebar_start_collapsed = true
sidebar_collapsed_mode = "hidden"
```

## Use

| Key | Action |
|---|---|
| `j` / `k` or arrows | Move |
| Enter or click | Jump to that agent or space |
| `/` or `prefix+/` | Find: type to filter, Esc to clear |
| `q` or Esc | Close the list and keep it closed in this tab |
| `prefix+a` | Open, focus, or close the list |

- Find matches every word you type against the session name, agent, title, status, tab, folder and space, so `docs blocked` works. Panes without an agent aren't listed.
- One list follows you: it moves to the tab you switch to, and focus stays on your work. Other tabs use their full width until you visit them.
- Spaces that need you sort to the top: blocked first, then done. Set `SORT_BY_ATTENTION = False` in `spaces_list.py` to keep herdr's order.
- With herdr's `one-dark` theme, the list uses that theme's colors. Other themes use your terminal's palette.
- The highlight follows the focused pane, except while the list itself has focus.
- Claude Code sessions show their `/rename` name and `/color`. These are read from Claude Code's internal files under `~/.claude`, so an update could change them; rows then fall back to the default text.

## Known limitations

- Switching tabs causes a brief flicker of about 60 ms. Herdr draws a tab before telling plugins about the switch, so the tab appears at full width, then narrows when the list arrives and its agent repaints at the new width. A faster hook only shortens this. Avoiding it would need a placeholder column in every tab so the layout never changes.
- Closing a tab may not open the list in the tab you land on until you next switch to it.
- Closing the list with `prefix+x` brings it back on your next visit to that tab. Use `q` or the toggle instead.
- `q` keeps the list closed by tab ID, and tab IDs repeat across herdr sessions, so it can also keep the list closed in another session's tab with the same ID.
- A tab whose right column is already split gives the list only the top slot.

## Develop

```bash
python3 -m unittest
herdr plugin link "$PWD"
```

Open lists reload themselves when `spaces_list.py` or herdr's config changes.

## License

[MIT](LICENSE)
