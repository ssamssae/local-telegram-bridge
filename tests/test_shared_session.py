import importlib.util
import io
from pathlib import Path
import tempfile
import threading
import unittest

ROOT = Path(__file__).parents[1]
spec = importlib.util.spec_from_file_location('local_bridge_shared', ROOT / 'local_bridge.py')
lb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lb)
terminal_spec = importlib.util.spec_from_file_location('terminal_chat', ROOT / 'terminal_chat.py')
terminal = importlib.util.module_from_spec(terminal_spec)
terminal_spec.loader.exec_module(terminal)


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

    def chat(self, profile, messages):
        self.calls.append((profile, messages))
        return 'answer:' + messages[-1]['content']


class SharedSessionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = Path(self.tmp.name)
        self.config = {
            'owner_id': 42, 'default_profile': 'qwen', 'fixed_profile': 'qwen',
            'history_turns': 3, 'max_tokens': 64, 'unload_other_profiles': True,
            'state_file': str(root / 'qwen-state.json'),
            'session_db': str(root / 'sessions.sqlite3'),
            'profiles': {
                '20b': {'provider': 'lmstudio', 'label': '20B', 'model': '20b',
                        'base_url': 'http://127.0.0.1:1234'},
                'qwen': {'provider': 'lmstudio', 'label': 'Qwen', 'model': 'qwen',
                         'base_url': 'http://127.0.0.1:1234'},
            },
        }
        self.telegram, self.models = Telegram(), Models()
        self.bridge = lb.Bridge(self.config, self.telegram, self.models,
                                bridge_id='qwen-bot')

    def test_terminal_request_uses_bridge_and_mirrors_both_sides(self):
        request_id, created = self.bridge.store.enqueue(
            'qwen', 'terminal', 'client:1', 'chat', '터미널 질문')
        self.assertTrue(created)
        self.assertTrue(self.bridge.process_pending())
        self.bridge.flush()
        self.assertEqual('qwen', self.models.calls[0][0])
        self.assertEqual(['user', 'assistant'],
                         [row['role'] for row in self.bridge.store.history('qwen')])
        events = self.bridge.store.events_since('qwen', 0)
        self.assertEqual(['user', 'assistant'], [row['kind'] for row in events])
        self.assertEqual(request_id, events[0]['request_id'])
        self.assertEqual(['[Terminal]\n터미널 질문', '[Qwen]\nanswer:터미널 질문'],
                         self.telegram.sent)

    def test_telegram_question_is_visible_in_terminal_event_journal(self):
        update = {'update_id': 9, 'message': {'text': '텔레그램 질문',
                  'from': {'id': 42}, 'chat': {'id': 42, 'type': 'private'}}}
        self.bridge.handle(update)
        events = self.bridge.store.events_since('qwen', 0)
        self.assertEqual(('user', 'telegram', '텔레그램 질문'),
                         (events[0]['kind'], events[0]['source'], events[0]['content']))
        self.assertEqual('answer:텔레그램 질문', events[1]['content'])

    def test_terminal_clear_is_profile_scoped_and_mirrored(self):
        other = dict(self.config, fixed_profile='20b', default_profile='20b',
                     state_file=str(Path(self.tmp.name) / '20b-state.json'))
        other_bridge = lb.Bridge(other, self.telegram, self.models, bridge_id='20b-bot')
        other_bridge.store.enqueue('20b', 'terminal', '20b:1', 'chat', 'keep')
        other_bridge.process_pending()
        self.bridge.store.enqueue('qwen', 'terminal', 'qwen:1', 'chat', 'remove')
        self.bridge.process_pending()
        self.bridge.store.enqueue('qwen', 'terminal', 'qwen:2', 'clear', '')
        self.bridge.process_pending()
        self.assertEqual([], self.bridge.store.history('qwen'))
        self.assertEqual(2, len(self.bridge.store.history('20b')))
        self.assertEqual('clear', self.bridge.store.events_since('qwen', 0)[-1]['kind'])

    def test_duplicate_concurrent_enqueue_creates_one_request(self):
        barrier = threading.Barrier(6)
        results = []

        def add():
            barrier.wait()
            results.append(self.bridge.store.enqueue(
                'qwen', 'terminal', 'same-key', 'chat', 'once'))

        threads = [threading.Thread(target=add) for _ in range(6)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(5)
        self.assertFalse(any(thread.is_alive() for thread in threads))
        self.assertEqual(1, sum(created for _, created in results))
        self.assertEqual(1, len({request_id for request_id, _ in results}))

    def test_running_request_is_requeued_after_restart(self):
        request_id, _ = self.bridge.store.enqueue(
            'qwen', 'terminal', 'restart:1', 'chat', 'resume')
        self.assertEqual(request_id, self.bridge.store.claim(['qwen'])['id'])
        restored = lb.SessionStore(self.config['session_db'])
        self.assertEqual(1, restored.recover(['qwen']))
        self.assertEqual(request_id, restored.claim(['qwen'])['id'])

    def test_fixed_profile_worker_does_not_claim_peer_queue(self):
        self.bridge.store.enqueue('20b', 'terminal', 'peer:1', 'chat', 'peer')
        self.assertFalse(self.bridge.process_pending())
        self.assertEqual([], self.models.calls)
        self.assertEqual('20b', self.bridge.store.claim(['20b'])['profile'])

    def test_session_database_is_private(self):
        self.assertEqual(0o600, Path(self.config['session_db']).stat().st_mode & 0o777)
        self.assertEqual(0o700, Path(self.config['session_db']).parent.stat().st_mode & 0o777)


class ConsoleTests(unittest.TestCase):
    def test_async_event_restores_prompt_and_partial_input(self):
        class Editor:
            @staticmethod
            def get_line_buffer():
                return '작성 중'

        stream = io.StringIO()
        console = terminal.Console('qwen › ', stream=stream, line_editor=Editor())
        console.begin_input()
        console.event('Telegram › 새 질문')
        self.assertEqual('\r\x1b[2KTelegram › 새 질문\nqwen › 작성 중', stream.getvalue())

    def test_fixed_config_rejects_another_profile(self):
        config = {'fixed_profile': 'qwen', 'default_profile': 'qwen',
                  'profiles': {'20b': {}, 'qwen': {}}}
        with self.assertRaises(terminal.BridgeError):
            terminal.choose_profile(config, '20b')


if __name__ == '__main__':
    unittest.main()
