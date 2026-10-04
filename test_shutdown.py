import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock, patch

import djamp


# Exercise the real main/PlayerProcess lifecycle in a separate process. Only
# terminal, network, and audio boundaries are stubbed; no Spotify state is used.
RUNNER = """
import os
from pathlib import Path
import signal
import sys
import time
from unittest.mock import Mock, patch
import djamp

root, mode = Path(sys.argv[1]), sys.argv[2]
sys.argv = ['djamp']
djamp.ROOT = root
djamp.CONFIG = root / 'config'
djamp.LOG = root / 'state/player.log'
djamp.API = lambda: Mock(request=Mock(return_value={'playback_ready': True})) if mode == 'attached' else Mock(request=Mock(side_effect=ConnectionRefusedError))
djamp.PlayerWorker = lambda api, **kwargs: Mock()
djamp.AudioMonitor = lambda: Mock()
djamp.LibraryWorker = lambda api: Mock()

def wait_for_stop(function, *args, **kwargs):
    (root / 'ui.ready').touch()
    while not args[-1]():
        time.sleep(0.01)

if mode == 'startup':
    popen = djamp.subprocess.Popen
    def start_and_signal(*args, **kwargs):
        child = popen(*args, **kwargs)
        (root / 'child.pid').write_text(str(child.pid))
        os.kill(os.getpid(), signal.SIGHUP)
        return child
    djamp.subprocess.Popen = start_and_signal

with patch.object(sys.stdin, 'isatty', return_value=True), patch.object(sys.stdout, 'isatty', return_value=True), patch.object(djamp.curses, 'wrapper', side_effect=wait_for_stop):
    raise SystemExit(djamp.main())
"""


class TestShutdown(unittest.TestCase):
    def stop_process(self, process):
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        if process.stderr:
            process.stderr.close()

    def wait_for(self, condition, process):
        deadline = time.monotonic() + 5
        while not condition():
            if process.poll() is not None:
                self.fail(f'DJamp exited before ready: {process.stderr.read()}')
            if time.monotonic() >= deadline:
                self.fail('DJamp did not become ready')
            time.sleep(0.01)

    def exercise_exit(self, signum, mode):
        with tempfile.TemporaryDirectory(prefix='djamp-test-shutdown-') as directory:
            root = Path(directory)
            binary = root / '.local/bin/go-librespot-djamp'
            binary.parent.mkdir(parents=True)
            binary.write_text(
                f'#!{sys.executable}\n'
                'import os, pathlib, signal, sys, time\n'
                'signal.signal(signal.SIGINT, lambda *_: sys.exit(0))\n'
                'pathlib.Path(__file__).parents[2].joinpath("child.pid").write_text(str(os.getpid()))\n'
                'time.sleep(60)\n'
            )
            binary.chmod(0o700)
            attached = None
            child_pid = None
            process = None
            try:
                if mode == 'attached':
                    attached = subprocess.Popen([str(binary)], start_new_session=True)
                    child_pid = attached.pid
                process = subprocess.Popen(
                    [sys.executable, '-c', RUNNER, directory, mode],
                    cwd=Path(__file__).resolve().parent, stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                    start_new_session=True,
                )
                if mode != 'startup':
                    self.wait_for(lambda: (root / 'ui.ready').exists() and (root / 'child.pid').exists(), process)
                    child_pid = int((root / 'child.pid').read_text())
                    process.send_signal(signum)
                self.assertEqual(process.wait(timeout=8), 0, process.stderr.read())
                child_pid = int((root / 'child.pid').read_text())
                if mode == 'attached':
                    self.assertIsNone(attached.poll(), 'attached backend was stopped')
                else:
                    with self.assertRaises(ProcessLookupError, msg='owned backend survived exit'):
                        os.kill(child_pid, 0)
            finally:
                if process is not None:
                    self.stop_process(process)
                if child_pid is None and (root / 'child.pid').exists():
                    child_pid = int((root / 'child.pid').read_text())
                if attached is not None:
                    self.stop_process(attached)
                elif child_pid is not None:
                    # Also clean up an orphan if the regression reappears.
                    try:
                        os.kill(child_pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass

    def test_signals_stop_owned_backend(self):
        for signum in (signal.SIGHUP, signal.SIGTERM, signal.SIGINT):
            with self.subTest(signal=signum):
                self.exercise_exit(signum, 'owned')

    def test_signals_leave_attached_backend_running(self):
        for signum in (signal.SIGHUP, signal.SIGTERM):
            with self.subTest(signal=signum):
                self.exercise_exit(signum, 'attached')

    def test_hangup_during_spawn_preserves_cleanup(self):
        self.exercise_exit(signal.SIGHUP, 'startup')

    def test_shutdown_cancels_link_prompt(self):
        window = Mock()
        window.getmaxyx.return_value = (24, 80)
        window.get_wch.side_effect = djamp.curses.error
        with patch('djamp.curses.curs_set'):
            self.assertEqual(djamp.prompt_link(window, {}, Mock(side_effect=[False, True])), '')
        window.timeout.assert_called_with(50)


if __name__ == '__main__':
    unittest.main()
