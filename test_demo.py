import curses
import unittest
from unittest.mock import Mock, patch

import djamp


class TestDemoControls(unittest.TestCase):
    def run_demo(self, events):
        now = [100.0]
        events = iter(events)
        states = []

        def read_key():
            now[0], key = next(events)
            if key is None:
                raise curses.error()
            return key

        window = Mock()
        window.get_wch.side_effect = read_key
        with patch("djamp.time.monotonic", side_effect=lambda: now[0]), \
                patch("djamp.curses.curs_set"), patch("djamp.palette", return_value={}), \
                patch("djamp.render", side_effect=lambda canvas, state, *args: states.append(state)):
            # None services ensure the offline preview cannot depend on the backend or audio capture.
            djamp.run_ui(window, None, None, None, demo=True)
        return [state["status"] for state in states]

    def test_pause_freezes_progress_and_resume_excludes_paused_time(self):
        states = self.run_demo([(102, " "), (112, None), (120, " "), (124, None),
                                (125, " "), (130, None), (132, " "), (134, "q")])
        self.assertEqual([state["track"]["position"] for state in states],
                         [0, 2000, 2000, 2000, 6000, 7000, 7000, 7000])
        self.assertEqual([state["paused"] for state in states],
                         [False, True, True, False, False, True, True, False])

    def test_mute_restores_adjusted_demo_volume(self):
        states = self.run_demo([(101, "+"), (102, "m"), (103, None), (104, "m"), (105, "q")])
        self.assertEqual([state["volume"] for state in states], [65, 70, 0, 0, 70])


if __name__ == "__main__":
    unittest.main()
