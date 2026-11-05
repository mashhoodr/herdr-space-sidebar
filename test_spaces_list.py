import unittest

from spaces_list import (
    build_rows,
    event_tab,
    followed_key,
    has_room,
    leftmost_pane,
    list_pane_in_tab,
    only_helpers_left,
    opening_key,
    parse_identity,
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
            [("space", "w1"), ("agent", "w1:p1"),
             ("space", "w2"),
             ("space", "w3"), ("agent", "w3:p1"), ("agent", "w3:p3")],
        )

    def test_space_row_carries_rollup_state(self):
        docs = build_rows(snapshot())[3]
        self.assertEqual(docs["text"], "docs")
        self.assertEqual(docs["state"], "done")

    def test_tab_number_only_shown_when_space_has_several_tabs(self):
        rows = {r["target"]: r["text"] for r in build_rows(snapshot())}
        self.assertEqual(rows["w1:p1"], "claude · test")
        self.assertEqual(rows["w3:p3"], "t2 claude · later tab")

    def test_title_matching_agent_name_is_not_repeated(self):
        rows = {r["target"]: r["text"] for r in build_rows(snapshot())}
        self.assertEqual(rows["w3:p1"], "t1 codex")

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

    def test_session_name_keeps_tab_prefix(self):
        identities = {"w3:p3": {"name": "later", "color": None}}
        rows = {r["target"]: r for r in build_rows(snapshot(), identities)}
        self.assertEqual(rows["w3:p3"]["text"], "t2 later")

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


class Layout(unittest.TestCase):
    def test_leftmost_pane_prefers_topmost_of_the_left_column(self):
        self.assertEqual(leftmost_pane(snapshot(), "w1:t1")["pane_id"], "w1:p1")

    def test_leftmost_pane_none_for_unknown_tab(self):
        self.assertIsNone(leftmost_pane(snapshot(), "w9:t1"))

    def test_list_pane_found_by_label_within_the_tab(self):
        self.assertEqual(list_pane_in_tab(snapshot(), "w1:t1"), "w1:p9")
        self.assertIsNone(list_pane_in_tab(snapshot(), "w3:t1"))


if __name__ == "__main__":
    unittest.main()
