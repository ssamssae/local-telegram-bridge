import fcntl
import json
import os
from pathlib import Path
import pty
import select
import signal
import struct
import sys
import tempfile
import termios
import threading
import time
import unittest

import local_bridge as lb


class Telegram:
    def __init__(self):
        self.sent = []

    def call(self, method, **payload):
        if method == 'sendChatAction':
            return True
        self.sent.append(payload['text'])
        return {'message_id': len(self.sent)}


class Models:
    def __init__(self):
        self.calls = []
        self.called = threading.Event()

    def chat(self, profile, messages):
        self.calls.append((profile, messages))
        self.called.set()
        return '확인:' + messages[-1]['content']


class RuntimeRegressionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = {
            'owner_id': 42, 'default_profile': 'qwen', 'fixed_profile': 'qwen',
            'history_turns': 6, 'max_tokens': 64, 'unload_other_profiles': True,
            'state_file': str(root / 'qwen.json'), 'session_db': str(root / 'chat.sqlite3'),
            'profiles': {name: {'label': name, 'provider': 'lmstudio', 'model': name,
                               'base_url': 'http://127.0.0.1:1234'}
                         for name in ['20b', 'qwen']}}
        self.telegram, self.models = Telegram(), Models()
        self.bridge = lb.Bridge(self.config, self.telegram, self.models)

    def message(self, update_id, text):
        return {'update_id': update_id, 'message': {'text': text,
                'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}}}

    def test_restart_after_clear_commit_before_json_snapshot(self):
        self.bridge.handle(self.message(1, 'old history'))
        self.bridge.flush()
        self.bridge.store.enqueue('qwen', 'terminal', 'clear:1', 'clear', '')
        request = self.bridge.store.claim(['qwen'])
        self.bridge.store.complete_clear(request, 'cleared', [])
        # Simulate a crash here: durable clear exists but JSON is still old.
        restored = lb.Bridge(self.config, self.telegram, self.models)
        self.assertEqual([], restored.store.history('qwen'))
        self.assertEqual(1, len(self.models.calls))

    def test_peer_clear_does_not_prevent_fixed_bot_restart(self):
        config = dict(self.config, fixed_profile='20b', default_profile='20b',
                      state_file=str(Path(self.tmp.name) / '20b.json'))
        other = lb.Bridge(config, self.telegram, self.models)
        other.handle(self.message(1, '20B history'))
        other.flush()
        self.bridge.handle(self.message(1, 'Qwen history'))
        self.bridge.flush()
        other.handle(self.message(2, '/clear'))
        other.flush()
        restored = lb.Bridge(self.config, self.telegram, self.models)
        self.assertEqual(2, len(restored.store.history('qwen')))
        self.assertEqual([], restored.store.history('20b'))

    def test_queued_input_does_not_bypass_required_legacy_migration(self):
        self.bridge.state['histories']['qwen'] = [
            {'role': 'user', 'content': 'legacy question'},
            {'role': 'assistant', 'content': 'legacy answer'}]
        self.bridge.save()
        self.bridge.store.enqueue('qwen', 'terminal', 'before-migration', 'chat', 'new input')
        with self.assertRaisesRegex(lb.BridgeError, 'migrate_history'):
            lb.Bridge(self.config, self.telegram, self.models)

    def test_terminal_request_is_not_blocked_by_idle_telegram_poll(self):
        entered, stop = threading.Event(), threading.Event()
        parent = self.telegram.call

        class StopLoop(BaseException):
            pass

        def call(method, **payload):
            if method != 'getUpdates':
                return parent(method, **payload)
            entered.set()
            if stop.wait(payload['timeout']):
                raise StopLoop()
            return []

        self.telegram.call = call

        def run():
            try:
                self.bridge.run()
            except StopLoop:
                pass

        thread = threading.Thread(target=run)
        thread.start()
        try:
            self.assertTrue(entered.wait(1))
            self.bridge.store.enqueue('qwen', 'terminal', 'idle:1', 'chat', 'hello')
            self.assertTrue(self.models.called.wait(1.8),
                            'Terminal input waited behind the Telegram long poll')
        finally:
            stop.set()
            thread.join(2)
        self.assertFalse(thread.is_alive())

    def test_real_pty_displays_incoming_chat_and_preserves_korean_input(self):
        config_path = Path(self.tmp.name) / 'config.json'
        config_path.write_text(json.dumps(self.config))
        script = Path(lb.__file__).with_name('terminal_chat.py')
        pid, master = pty.fork()
        if pid == 0:
            os.environ['TERM'] = 'xterm-256color'
            os.execv(sys.executable, [sys.executable, '-u', str(script),
                                    '--config', str(config_path), '--profile', 'qwen'])
        fcntl.ioctl(master, termios.TIOCSWINSZ, struct.pack('HHHH', 24, 53, 0, 0))
        received = bytearray()

        def wait_text(text, timeout=5):
            until = time.monotonic() + timeout
            target = text.encode()
            while target not in received and time.monotonic() < until:
                readable, _, _ = select.select([master], [], [], .1)
                if readable:
                    received.extend(os.read(master, 65536))
            self.assertIn(target, received)

        try:
            wait_text('qwen › ')
            os.write(master, '작성중'.encode())
            wait_text('작성중')
            self.bridge.handle(self.message(1, '텔레그램 인사'))
            self.bridge.flush()
            wait_text('Telegram › 텔레그램 인사')
            wait_text('qwen › 확인:텔레그램 인사')
            os.write(master, bytes([13]))
            until = time.monotonic() + 3
            request = None
            while request is None and time.monotonic() < until:
                if select.select([master], [], [], .02)[0]:
                    received.extend(os.read(master, 65536))
                request = self.bridge.store.claim(['qwen'])
            while select.select([master], [], [], 0)[0]:
                received.extend(os.read(master, 65536))
            self.assertIsNotNone(request, received.decode(errors='replace'))
            self.assertEqual('작성중', request['content'])
            self.bridge.store.complete_chat(request, '입력 보존', 6, [])
            wait_text('qwen › 입력 보존')
            os.write(master, b'/exit' + bytes([13]))
        finally:
            os.close(master)
            try:
                os.kill(pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            os.waitpid(pid, 0)
