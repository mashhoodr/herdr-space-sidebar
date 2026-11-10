import itertools
import json
import os
import subprocess
import tempfile
import unittest
from unittest import mock

from spaces_list import (
    ONE_DARK,
    PAD_Y,
    agent_text,
    attention_sorted,
    build_rows,
    configured_palette,
    ESCAPE,
    display_lines,
    edit_query,
    ellipsize,
    event_tab,
    filter_rows,
    first_match,
    followed_key,
    has_room,
    is_preview,
    pane_changed,
    list_pane_in_tab,
    list_process_state,
    nearest_256,
    only_helpers_left,
    opening_key,
    parse_identity,
    row_at,
    scroll_top,
    source_changed,
    stale_list_panes,
    text_width,
    latest_target,
    locate_self,
    move_list,
    remember_target,
    rightmost_pane,
    sync_list,
    user_session_name,
)


def snapshot():
    return {
        "focused_workspace_id": "w1",
        "focused_pane_id": "w1:p1",
        "workspaces": [
            {"workspace_id": "w1", "label": "web-app", "agent_status": "working",
             "tab_count": 1, "active_tab_id": "w1:t1"},
            {"workspace_id": "w2", "label": "api", "agent_status": "unknown",
             "tab_count": 1, "active_tab_id": "w2:t1"},
            {"workspace_id": "w3", "label": "docs", "agent_status": "done",
             "tab_count": 2, "active_tab_id": "w3:t1"},
        ],
        "tabs": [
            {"tab_id": "w1:t1", "number": 1},
            {"tab_id": "w2:t1", "number": 1},
            {"tab_id": "w3:t1", "number": 1},
            {"tab_id": "w3:t2", "number": 2},
        ],
        "agents": [
            {"pane_id": "w3:p3", "workspace_id": "w3", "tab_id": "w3:t2", "agent": "claude",
             "agent_status": "done", "terminal_title_stripped": "later tab"},
            {"pane_id": "w1:p1", "workspace_id": "w1", "tab_id": "w1:t1", "agent": "claude",
             "agent_status": "working", "terminal_title_stripped": "test"},
            {"pane_id": "w3:p1", "workspace_id": "w3", "tab_id": "w3:t1", "agent": "codex",
             "agent_status": "idle", "terminal_title_stripped": "codex"},
        ],
        "panes": [
            {"pane_id": "w1:p1", "tab_id": "w1:t1"},
            {"pane_id": "w1:p9", "tab_id": "w1:t1", "label": "Spaces"},
        ],
        "layouts": [
            {"tab_id": "w1:t1", "panes": [
                {"pane_id": "w1:p2", "rect": {"x": 50, "y": 0, "width": 50, "height": 40}},
                {"pane_id": "w1:p3", "rect": {"x": 0, "y": 20, "width": 50, "height": 20}},
                {"pane_id": "w1:p1", "rect": {"x": 0, "y": 0, "width": 50, "height": 20}},
            ]},
        ],
    }


class BuildRows(unittest.TestCase):
    def test_nests_agents_directly_under_their_space_in_space_order(self):
        rows = build_rows(snapshot())
        self.assertEqual(
            [(r["kind"], r["target"]) for r in rows],
            [("space", "w3"), ("agent", "w3:p1"), ("agent", "w3:p3"),
             ("space", "w1"), ("agent", "w1:p1"),
             ("space", "w2")],
        )

    def test_space_row_carries_rollup_state(self):
        docs = next(r for r in build_rows(snapshot()) if r["target"] == "w3")
        self.assertEqual(docs["text"], "docs")
        self.assertEqual(docs["state"], "done")

    def test_tab_number_is_a_separate_detail_only_with_several_tabs(self):
        rows = {r["target"]: r for r in build_rows(snapshot())}
        self.assertEqual(rows["w1:p1"]["text"], "claude · test")
        self.assertEqual(rows["w1:p1"]["detail"], "")
        self.assertEqual(rows["w3:p3"]["text"], "claude · later tab")
        self.assertEqual(rows["w3:p3"]["detail"], "t2")

    def test_title_matching_agent_name_is_not_repeated(self):
        rows = {r["target"]: r["text"] for r in build_rows(snapshot())}
        self.assertEqual(rows["w3:p1"], "codex")

    def test_filler_title_is_hidden(self):
        agent = {"agent": "claude", "terminal_title_stripped": "Claude Code"}
        self.assertEqual(agent_text(agent), "claude")

    def test_marks_current_space_and_focused_agent(self):
        rows = {r["target"]: r for r in build_rows(snapshot())}
        self.assertTrue(rows["w1"]["current"])
        self.assertFalse(rows["w2"]["current"])
        self.assertTrue(rows["w1:p1"]["current"])
        self.assertFalse(rows["w3:p1"]["current"])

    def test_empty_snapshot_gives_no_rows(self):
        self.assertEqual(build_rows({}), [])


class SessionIdentity(unittest.TestCase):
    def test_last_name_and_color_win(self):
        text = "\n".join([
            '{"type":"agent-name","agentName":"old"}',
            '{"type":"agent-color","agentColor":"red"}',
            '{"type":"user","message":"agent-color is mentioned here"}',
            '{"type":"custom-title","customTitle":"new"}',
            '{"type":"agent-color","agentColor":"blue"}',
        ])
        self.assertEqual(parse_identity(text), {"name": "new", "color": "blue"})

    def test_ignores_partial_first_line_of_a_tail_read(self):
        text = 'gentColor":"red"}\n{"type":"agent-name","agentName":"kept"}'
        self.assertEqual(parse_identity(text), {"name": "kept", "color": None})

    def test_nothing_set_gives_empty_identity(self):
        self.assertEqual(parse_identity('{"type":"user"}'), {"name": None, "color": None})

    def test_only_user_set_session_names_are_used(self):
        self.assertEqual(user_session_name({"name": "mine", "nameSource": "user"}), "mine")
        self.assertIsNone(user_session_name({"name": "dir-bd", "nameSource": "derived"}))

    def test_session_name_replaces_agent_and_title_and_carries_color(self):
        identities = {"w1:p1": {"name": "my session", "color": "blue"}}
        rows = {r["target"]: r for r in build_rows(snapshot(), identities)}
        self.assertEqual(rows["w1:p1"]["text"], "my session")
        self.assertEqual(rows["w1:p1"]["color"], "blue")

    def test_session_name_keeps_tab_detail(self):
        identities = {"w3:p3": {"name": "later", "color": None}}
        rows = {r["target"]: r for r in build_rows(snapshot(), identities)}
        self.assertEqual(rows["w3:p3"]["text"], "later")
        self.assertEqual(rows["w3:p3"]["detail"], "t2")

    def test_color_without_a_name_keeps_the_default_text(self):
        identities = {"w1:p1": {"name": None, "color": "green"}}
        rows = {r["target"]: r for r in build_rows(snapshot(), identities)}
        self.assertEqual(rows["w1:p1"]["text"], "claude · test")
        self.assertEqual(rows["w1:p1"]["color"], "green")

    def test_agents_without_identity_have_no_color(self):
        rows = {r["target"]: r for r in build_rows(snapshot())}
        self.assertIsNone(rows["w1:p1"]["color"])


class Selection(unittest.TestCase):
    def test_follows_the_focused_agent(self):
        snap = snapshot()
        self.assertEqual(followed_key(snap, "w1:p9", build_rows(snap)), ("agent", "w1:p1"))

    def test_focus_on_a_non_agent_pane_selects_its_space(self):
        snap = dict(snapshot(), focused_pane_id="w3:p2", focused_workspace_id="w3")
        self.assertEqual(followed_key(snap, "w1:p9", build_rows(snap)), ("space", "w3"))

    def test_does_not_follow_while_the_list_has_focus(self):
        snap = dict(snapshot(), focused_pane_id="w1:p9")
        self.assertIsNone(followed_key(snap, "w1:p9", build_rows(snap)))

    def test_new_list_opens_on_its_tabs_agent(self):
        self.assertEqual(opening_key(snapshot(), "w1:p9", "w1:t1"), ("agent", "w1:p1"))

    def test_new_list_without_an_agent_opens_on_its_space(self):
        snap = snapshot()
        snap["panes"].append({"pane_id": "w2:p9", "tab_id": "w2:t1", "workspace_id": "w2"})
        self.assertEqual(opening_key(snap, "w2:p9", "w2:t1"), ("space", "w2"))


class FocusHook(unittest.TestCase):
    def test_tab_comes_from_the_event_not_global_focus(self):
        self.assertEqual(event_tab({"data": {"tab_id": "w3:t2"}}, snapshot()), "w3:t2")
        self.assertEqual(event_tab({"tab": {"tab_id": "w3:t2"}}, snapshot()), "w3:t2")

    def test_workspace_event_resolves_to_its_active_tab(self):
        self.assertEqual(event_tab({"data": {"workspace_id": "w3"}}, snapshot()), "w3:t1")
        self.assertEqual(
            event_tab({"workspace": {"workspace_id": "w2"}}, snapshot()), "w2:t1"
        )

    def test_empty_event_falls_back_to_focused_tab(self):
        snap = dict(snapshot(), focused_tab_id="w1:t1")
        self.assertEqual(event_tab({}, snap), "w1:t1")

    def test_room_requires_a_wide_unzoomed_known_tab(self):
        def snap(**layout):
            return {"layouts": [dict({"tab_id": "t"}, **layout)]}
        self.assertTrue(has_room(snap(area={"width": 200}), "t"))
        self.assertFalse(has_room(snap(area={"width": 60}), "t"))
        self.assertFalse(has_room(snap(area={"width": 200}, zoomed=True), "t"))
        self.assertFalse(has_room(snap(area={"width": 200}), "missing"))

    def test_only_helpers_left(self):
        def snap(*labels):
            return {"panes": [{"tab_id": "t", "label": label} for label in labels]}
        self.assertTrue(only_helpers_left(snap("Spaces", "Sidebar"), "t"))
        self.assertFalse(only_helpers_left(snap("Spaces", None), "t"))
        self.assertFalse(only_helpers_left(snap(), "t"))


class SingleList(unittest.TestCase):
    @staticmethod
    def snap(*panes):
        return {"panes": [
            {"pane_id": pane_id, "tab_id": tab_id, "label": label}
            for pane_id, tab_id, label in panes
        ]}

    def test_lists_in_other_tabs_are_stale(self):
        snap = self.snap(("a1", "a", "Spaces"), ("a2", "a", None),
                         ("b1", "b", "Spaces"), ("b2", "b", None))
        self.assertEqual(stale_list_panes(snap, "b"), ["a1"])

    def test_active_tab_and_other_panes_are_never_stale(self):
        snap = self.snap(("a1", "a", "Spaces"), ("a2", "a", None), ("c1", "c", None))
        self.assertEqual(stale_list_panes(snap, "a"), [])

    def test_closing_must_not_take_a_whole_tab_with_it(self):
        snap = self.snap(("a1", "a", "Spaces"), ("b1", "b", "Spaces"), ("b2", "b", "Sidebar"))
        self.assertEqual(stale_list_panes(snap, "c"), [])

    def test_latest_target_wins_and_missing_falls_back(self):
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": state, "HERDR_SOCKET_PATH": "/s/herdr.sock"}
        ):
            self.assertEqual(latest_target("fallback"), "fallback")
            remember_target("w1:t1")
            remember_target("w2:t3")
            self.assertEqual(latest_target("fallback"), "w2:t3")

    def test_targets_do_not_leak_between_sessions(self):
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": state, "HERDR_SOCKET_PATH": "/one/herdr.sock"}
        ):
            remember_target("w1:t1")
            os.environ["HERDR_SOCKET_PATH"] = "/two/herdr.sock"
            self.assertEqual(latest_target("fallback"), "fallback")


class LocateSelf(unittest.TestCase):
    SNAP = {"panes": [
        {"pane_id": "w2:p2", "tab_id": "w2:t1", "terminal_id": "term_a"},
        {"pane_id": "w1:p9", "tab_id": "w1:t1", "terminal_id": "term_b"},
    ]}

    def test_first_look_learns_the_terminal_from_the_pane_id(self):
        self.assertEqual(locate_self(self.SNAP, "w1:p9", "w1:t1", None), ("w1:p9", "w1:t1", "term_b"))

    def test_a_move_changes_pane_and_tab_but_not_the_terminal(self):
        self.assertEqual(locate_self(self.SNAP, "w1:p2", "w1:t1", "term_a"), ("w2:p2", "w2:t1", "term_a"))

    def test_a_recycled_pane_id_does_not_fool_a_known_terminal(self):
        self.assertEqual(locate_self(self.SNAP, "w1:p9", "w1:t1", "term_a"), ("w2:p2", "w2:t1", "term_a"))

    def test_not_found_keeps_what_it_knew(self):
        self.assertEqual(locate_self(self.SNAP, "w9:p1", "w9:t1", "term_z"), ("w9:p1", "w9:t1", "term_z"))


def move_result(pane_id):
    return json.dumps({"result": {"move_result": {"pane": {"pane_id": pane_id}}}})


class MoveList(unittest.TestCase):
    SNAP = {
        "layouts": [{"tab_id": "t2", "area": {"width": 200}, "panes": [
            {"pane_id": "work", "rect": {"x": 0, "y": 0, "width": 160}},
        ]}],
    }

    def test_splits_at_the_final_width_and_returns_the_new_id(self):
        with mock.patch("spaces_list.herdr", return_value=move_result("new")) as herdr, \
                mock.patch("spaces_list.correct_width"):
            self.assertEqual(move_list(self.SNAP, "old", "t2"), "new")
        self.assertEqual(herdr.call_args_list, [
            mock.call("pane", "move", "old", "--tab", "t2", "--split", "right",
                      "--target-pane", "work", "--ratio", "0.8000", "--no-focus"),
            mock.call("pane", "resize", "--pane", "new", "--direction", "right", "--amount", "0.0001"),
        ])

    def test_a_narrow_target_keeps_at_least_half(self):
        snap = {"layouts": [{"tab_id": "t2", "panes": [
            {"pane_id": "work", "rect": {"x": 0, "y": 0, "width": 40}},
        ]}]}
        with mock.patch("spaces_list.herdr", return_value=move_result("new")) as herdr, \
                mock.patch("spaces_list.correct_width"):
            move_list(snap, "old", "t2")
        self.assertIn("0.5000", herdr.call_args_list[0].args)

    def test_no_target_means_no_move(self):
        with mock.patch("spaces_list.herdr") as herdr:
            self.assertIsNone(move_list({"layouts": []}, "old", "t2"))
        herdr.assert_not_called()


class SyncList(unittest.TestCase):
    @staticmethod
    def snap(*panes, focused="a2", zoomed=False, width=180):
        tabs = {tab for _, tab, _ in panes}
        return {
            "focused_pane_id": focused,
            "panes": [{"pane_id": p, "tab_id": t, "label": l} for p, t, l in panes],
            "layouts": [{
                "tab_id": tab, "zoomed": zoomed, "area": {"width": width},
                "panes": [{"pane_id": p, "rect": {"x": 0, "y": 0, "width": width}}
                          for p, t, l in panes if t == tab],
            } for tab in tabs],
        }

    def run_sync(self, snap, tab="b", move=None, closed_marker=False):
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(
            os.environ, {"HERDR_PLUGIN_STATE_DIR": state}
        ), mock.patch("spaces_list.fetch_snapshot", return_value=snap), \
                mock.patch("spaces_list.herdr") as herdr, \
                mock.patch("spaces_list.rpc") as rpc, \
                mock.patch("spaces_list.revive") as revive, \
                mock.patch("spaces_list.dock", return_value="docked") as dock, \
                mock.patch("spaces_list.move_list", move or mock.Mock(return_value="moved")) as moved:
            if closed_marker:
                open(os.path.join(state, f"closed-{tab}"), "w").close()
            sync_list(tab)
        return mock.Mock(herdr=herdr, rpc=rpc, revive=revive, dock=dock, move=moved)

    @staticmethod
    def closed(result):
        return [c.args[2] for c in result.herdr.call_args_list if c.args[:2] == ("pane", "close")]

    def test_moves_the_running_list_instead_of_opening_one(self):
        result = self.run_sync(self.snap(("a1", "a", "Spaces"), ("a2", "a", None), ("b1", "b", None)))
        self.assertEqual(result.move.call_args.args[1:], ("a1", "b"))
        result.dock.assert_not_called()
        self.assertEqual(self.closed(result), [])

    def test_extra_lists_are_closed_and_one_is_moved(self):
        result = self.run_sync(self.snap(("a1", "a", "Spaces"), ("a2", "a", None),
                                         ("c1", "c", "Spaces"), ("c2", "c", None), ("b1", "b", None)))
        self.assertEqual(result.move.call_args.args[1:], ("a1", "b"))
        self.assertEqual(self.closed(result), ["c1"])

    def test_no_list_anywhere_opens_one(self):
        result = self.run_sync(self.snap(("a2", "a", None), ("b1", "b", None)))
        result.move.assert_not_called()
        result.dock.assert_called_once()

    def test_a_list_already_here_stays_and_others_close(self):
        result = self.run_sync(self.snap(("a1", "a", "Spaces"), ("a2", "a", None),
                                         ("b0", "b", "Spaces"), ("b1", "b", None)))
        result.move.assert_not_called()
        result.revive.assert_called_once_with("b0")
        self.assertEqual(self.closed(result), ["a1"])

    def test_a_tab_closed_with_q_gets_no_list(self):
        result = self.run_sync(self.snap(("a1", "a", "Spaces"), ("a2", "a", None), ("b1", "b", None)),
                               closed_marker=True)
        result.move.assert_not_called()
        result.dock.assert_not_called()
        self.assertEqual(self.closed(result), ["a1"])

    def test_a_narrow_tab_gets_no_list(self):
        result = self.run_sync(self.snap(("a1", "a", "Spaces"), ("a2", "a", None), ("b1", "b", None), width=80))
        result.move.assert_not_called()
        self.assertEqual(self.closed(result), ["a1"])

    def test_a_failed_move_falls_back_to_opening_one(self):
        failing = mock.Mock(side_effect=subprocess.CalledProcessError(1, "herdr"))
        result = self.run_sync(self.snap(("a1", "a", "Spaces"), ("a2", "a", None), ("b1", "b", None)), move=failing)
        self.assertEqual(self.closed(result), ["a1"])
        result.dock.assert_called_once()

    def test_focus_returns_to_work_if_the_list_took_it(self):
        snap = self.snap(("a1", "a", "Spaces"), ("a2", "a", None), ("b1", "b", None), focused="moved")
        result = self.run_sync(snap)
        result.rpc.assert_called_once_with("pane.focus", {"pane_id": "b1"})

    def test_a_tab_with_a_preview_gets_no_list(self):
        result = self.run_sync(self.snap(("a1", "a", "Spaces"), ("a2", "a", None),
                                         ("b1", "b", None), ("b2", "b", "notes.md · preview")))
        result.move.assert_not_called()
        result.dock.assert_not_called()
        self.assertEqual(self.closed(result), ["a1"])


class Preview(unittest.TestCase):
    def test_previews_are_known_by_token_or_name(self):
        self.assertTrue(is_preview({"label": "notes.md", "tokens": {"herdr-sidebar-preview": "1"}}))
        self.assertTrue(is_preview({"label": "notes.md · preview"}))
        self.assertTrue(is_preview({"label": "main.rs · editor"}))
        self.assertTrue(is_preview({"label": "Sidebar", "tokens": {"herdr-sidebar-explorer": "1"}}))
        self.assertTrue(is_preview({"label": "Source Control"}))
        self.assertFalse(is_preview({"label": "herdr-dashboard"}))
        self.assertFalse(is_preview({"label": "Spaces"}))
        self.assertFalse(is_preview({}))

    def run_event(self, event, snaps, aside=None, closed=None):
        with tempfile.TemporaryDirectory() as state, mock.patch.dict(os.environ, {
            "HERDR_PLUGIN_STATE_DIR": state, "HERDR_PLUGIN_EVENT_JSON": json.dumps(event),
        }), mock.patch("spaces_list.fetch_snapshot", side_effect=snaps), \
                mock.patch("spaces_list.time.sleep"), \
                mock.patch("spaces_list.time.monotonic", side_effect=itertools.count(0, 0.5)), \
                mock.patch("spaces_list.herdr") as herdr, \
                mock.patch("spaces_list.sync_list") as sync:
            for kind, tab in (("aside", aside), ("closed", closed)):
                if tab:
                    open(os.path.join(state, f"{kind}-{tab}"), "w").close()
            pane_changed()
            remaining = sorted(os.listdir(state))
        return mock.Mock(herdr=herdr, sync=sync, markers=remaining)

    @staticmethod
    def snap(*panes, focused_tab="b"):
        return {"focused_tab_id": focused_tab,
                "panes": [{"pane_id": p, "tab_id": "b", "label": l} for p, l in panes]}

    def created(self, pane_id):
        return {"event": "pane_created", "data": {"type": "pane_created", "pane": {"pane_id": pane_id}}}

    def test_a_new_preview_closes_the_list_and_remembers_why(self):
        unnamed = self.snap(("b1", None), ("b0", "Spaces"), ("b2", None))
        named = self.snap(("b1", None), ("b0", "Spaces"), ("b2", "notes.md · preview"))
        result = self.run_event(self.created("b2"), [unnamed, named, named])
        result.herdr.assert_called_once_with("pane", "close", "b0")
        self.assertIn("aside-b", result.markers)

    def test_a_pane_that_never_becomes_a_preview_is_left_alone(self):
        plain = self.snap(("b1", None), ("b0", "Spaces"), ("b2", None))
        result = self.run_event(self.created("b2"), [plain] * 100)
        result.herdr.assert_not_called()

    def test_a_list_closed_with_q_stays_closed_when_a_preview_opens(self):
        named = self.snap(("b1", None), ("b0", "Spaces"), ("b2", "notes.md · preview"))
        result = self.run_event(self.created("b2"), [named] * 3, closed="b")
        result.herdr.assert_not_called()

    def test_closing_the_preview_brings_the_list_back(self):
        closed = {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "b2"}}
        after = self.snap(("b1", None))
        result = self.run_event(closed, [after, after], aside="b")
        result.sync.assert_called_once_with("b")

    def test_closing_a_pane_elsewhere_does_nothing(self):
        closed = {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "b2"}}
        result = self.run_event(closed, [self.snap(("b1", None))])
        result.sync.assert_not_called()

    def test_closing_one_of_two_previews_keeps_the_list_away(self):
        closed = {"event": "pane_closed", "data": {"type": "pane_closed", "pane_id": "b2"}}
        after = self.snap(("b1", None), ("b3", "other.md · preview"))
        result = self.run_event(closed, [after, after], aside="b")
        result.sync.assert_not_called()


class RestoredPanes(unittest.TestCase):
    def test_list_process_counts_as_running(self):
        procs = [{"argv0": "python3", "cmdline": "python3 spaces_list.py"}]
        self.assertEqual(list_process_state(procs), "running")

    def test_a_bare_shell_can_be_revived(self):
        self.assertEqual(list_process_state([{"argv0": "fish", "cmdline": "fish"}]), "shell")
        self.assertEqual(list_process_state([{"argv0": "-zsh", "cmdline": "-zsh"}]), "shell")
        self.assertEqual(list_process_state([{"argv0": "/bin/bash"}]), "shell")

    def test_anything_else_is_left_alone(self):
        self.assertEqual(list_process_state([{"argv0": "vim", "cmdline": "vim notes"}]), "other")
        self.assertEqual(list_process_state([{"argv0": "fish"}, {"argv0": "git"}]), "other")

    def test_no_processes_yet_is_not_a_shell(self):
        self.assertEqual(list_process_state([]), "other")


class Theme(unittest.TestCase):
    def test_exact_palette_colors_map_exactly(self):
        self.assertEqual(nearest_256("#5fafff"), 75)
        self.assertEqual(nearest_256("#000000"), 16)
        self.assertEqual(nearest_256("#808080"), 244)

    def test_near_colors_pick_the_closest(self):
        self.assertEqual(nearest_256("#60b0f0"), 75)

    def test_config_selects_a_known_theme(self):
        self.assertIs(configured_palette('[theme]\nname = "one-dark"\n'), ONE_DARK)

    def test_other_or_missing_themes_keep_the_defaults(self):
        self.assertIsNone(configured_palette('[theme]\nname = "nord"\n'))
        self.assertIsNone(configured_palette("onboarding = false\n"))

    def test_unreadable_config_keeps_the_defaults(self):
        self.assertIsNone(configured_palette("not [valid toml"))


class SelfReload(unittest.TestCase):
    def test_reloads_only_on_a_real_change(self):
        self.assertFalse(source_changed(100.0, 100.0))
        self.assertTrue(source_changed(100.0, 101.0))

    def test_missing_file_does_not_reload(self):
        self.assertFalse(source_changed(100.0, None))


class Find(unittest.TestCase):
    def rows(self):
        return build_rows(snapshot(), {"w3:p3": {"name": "changelog", "color": None}})

    def targets(self, query):
        return [r["target"] for r in filter_rows(self.rows(), query)]

    def test_empty_query_keeps_everything(self):
        self.assertEqual(len(self.targets("  ")), len(self.rows()))

    def test_agent_match_keeps_its_space_and_drops_siblings(self):
        self.assertEqual(self.targets("changelog"), ["w3", "w3:p3"])

    def test_space_match_keeps_all_its_agents(self):
        self.assertEqual(self.targets("docs"), ["w3", "w3:p1", "w3:p3"])

    def test_every_word_must_match_across_space_and_agent(self):
        self.assertEqual(self.targets("docs codex"), ["w3", "w3:p1"])
        self.assertEqual(self.targets("api codex"), [])

    def test_status_word_selects_agents_in_that_state(self):
        self.assertEqual(self.targets("working"), ["w1", "w1:p1"])

    def test_match_ignores_case(self):
        self.assertEqual(self.targets("CHANGELOG"), ["w3", "w3:p3"])

    def test_cursor_lands_on_first_agent_when_there_is_one(self):
        self.assertEqual(first_match(filter_rows(self.rows(), "changelog")), 1)
        self.assertEqual(first_match([{"kind": "space"}]), 0)
        self.assertEqual(first_match([]), 0)

    def test_typing_editing_and_leaving(self):
        self.assertEqual(edit_query("ab", ord("c")), "abc")
        self.assertEqual(edit_query("ab", 127), "a")
        self.assertEqual(edit_query("", 127), "")
        self.assertEqual(edit_query("ab", 21), "")
        self.assertIsNone(edit_query("ab", ESCAPE))
        self.assertEqual(edit_query("ab", 500), "ab", "other keys change nothing")


class Attention(unittest.TestCase):
    def test_blocked_then_done_then_the_rest_in_original_order(self):
        spaces = [{"agent_status": s, "n": i} for i, s in
                  enumerate(["idle", "done", "working", "blocked", "done"])]
        self.assertEqual([s["n"] for s in attention_sorted(spaces)], [3, 1, 4, 0, 2])


class Fitting(unittest.TestCase):
    def test_short_text_is_untouched(self):
        self.assertEqual(ellipsize("short", 5), "short")

    def test_long_text_ends_in_ellipsis_within_the_limit(self):
        result = ellipsize("release-notes-long", 8)
        self.assertEqual(result, "release…")
        self.assertEqual(text_width(result), 8)

    def test_no_dangling_space_before_ellipsis(self):
        self.assertEqual(ellipsize("codex · lint", 7), "codex…")

    def test_no_room_gives_nothing(self):
        self.assertEqual(ellipsize("text", 0), "")

    def test_wide_characters_count_double(self):
        self.assertEqual(text_width("日本"), 4)
        self.assertLessEqual(text_width(ellipsize("日本語のタイトル", 6)), 6)


class Spacing(unittest.TestCase):
    ROWS = [{"kind": "space"}, {"kind": "agent"}, {"kind": "space"}, {"kind": "space"}, {"kind": "agent"}]

    def test_gap_before_every_space_but_the_first(self):
        self.assertEqual(display_lines(self.ROWS), [0, 1, None, 2, None, 3, 4])

    def test_no_rows_no_lines(self):
        self.assertEqual(display_lines([]), [])

    def test_click_maps_past_gaps_and_padding(self):
        lines = display_lines(self.ROWS)
        self.assertEqual(row_at(lines, 0, PAD_Y), 0)
        self.assertEqual(row_at(lines, 0, PAD_Y + 3), 2)
        self.assertIsNone(row_at(lines, 0, PAD_Y + 2), "a gap is not a row")
        self.assertIsNone(row_at(lines, 0, PAD_Y - 1), "padding is not a row")
        self.assertIsNone(row_at(lines, 0, PAD_Y + 7), "below the list")

    def test_click_accounts_for_scrolling(self):
        lines = display_lines(self.ROWS)
        self.assertEqual(row_at(lines, 3, PAD_Y), 2)

    def test_scroll_keeps_cursor_visible_counting_gaps(self):
        lines = display_lines(self.ROWS)
        self.assertEqual(scroll_top(lines, 4, 0, 3), 4)
        self.assertEqual(scroll_top(lines, 0, 4, 3), 0)
        self.assertEqual(scroll_top(lines, 1, 0, 3), 0)

    def test_scroll_pulls_back_when_the_list_shrinks(self):
        self.assertEqual(scroll_top([0, 1], 0, 5, 3), 0)


class Layout(unittest.TestCase):
    def test_rightmost_pane_is_at_the_right_edge(self):
        self.assertEqual(rightmost_pane(snapshot(), "w1:t1")["pane_id"], "w1:p2")

    def test_rightmost_pane_prefers_topmost_of_the_right_column(self):
        snap = {"layouts": [{"tab_id": "t", "panes": [
            {"pane_id": "left", "rect": {"x": 0, "y": 0, "width": 50}},
            {"pane_id": "lower", "rect": {"x": 50, "y": 20, "width": 50}},
            {"pane_id": "upper", "rect": {"x": 50, "y": 0, "width": 50}},
        ]}]}
        self.assertEqual(rightmost_pane(snap, "t")["pane_id"], "upper")

    def test_rightmost_pane_none_for_unknown_tab(self):
        self.assertIsNone(rightmost_pane(snapshot(), "w9:t1"))

    def test_list_pane_found_by_label_within_the_tab(self):
        self.assertEqual(list_pane_in_tab(snapshot(), "w1:t1"), "w1:p9")
        self.assertIsNone(list_pane_in_tab(snapshot(), "w3:t1"))


if __name__ == "__main__":
    unittest.main()
