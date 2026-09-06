import copy
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
import urllib.error

spec = importlib.util.spec_from_file_location('local_bridge', Path(__file__).parents[1] / 'local_bridge.py')
lb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(lb)


class FakeTelegram:
    def __init__(self):
        self.sent = []
        self.fail = False

    def call(self, method, **payload):
        if method == 'sendChatAction':
            return True
        if self.fail:
            raise lb.BridgeError('HTTP 503')
        self.sent.append(payload['text'])
        return {'message_id': len(self.sent)}


class FakeModels:
    def __init__(self):
        self.calls = []
        self.fail = False

    def chat(self, name, messages):
        self.calls.append((name, messages))
        if self.fail:
            raise lb.BridgeError('HTTP 500')
        return '응답: ' + messages[-1]['content']


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = {'owner_id': 42, 'default_profile': '20b', 'history_turns': 2,
                       'max_tokens': 64, 'unload_other_profiles': True,
                       'state_file': str(Path(self.tmp.name) / 'state.json'),
                       'profiles': {
                           '20b': {'label': '20B', 'provider': 'ollama', 'model': 'test-20b', 'base_url': 'http://127.0.0.1:11434'},
                           'qwen': {'label': 'Qwen', 'provider': 'lmstudio', 'model': 'test-qwen', 'base_url': 'http://127.0.0.1:1234'}}}
        self.telegram, self.models = FakeTelegram(), FakeModels()
        self.bridge = lb.Bridge(self.config, self.telegram, self.models)

    def update(self, number, text, sender=42, chat=42, kind='private'):
        return {'update_id': number, 'message': {'text': text, 'from': {'id': sender},
                                                'chat': {'id': chat, 'type': kind}}}

    def submit(self, number, text):
        self.bridge.handle(self.update(number, text))
        self.bridge.flush()

    def test_unauthorized_sender_and_groups_cannot_call_models(self):
        for index, args in enumerate([{'sender': 1}, {'chat': 1}, {'kind': 'group'}]):
            self.bridge.handle(self.update(index, 'hello', **args))
        self.assertEqual([], self.models.calls)
        self.assertEqual([], self.telegram.sent)
        self.assertEqual(3, self.bridge.state['offset'])

    def test_profile_selection_and_histories_survive_restart(self):
        self.submit(1, '20B 전용')
        self.submit(2, '/qwen')
        self.submit(3, '퀜 전용')
        restored = lb.Bridge(self.config, self.telegram, self.models)
        self.assertEqual('qwen', restored.state['selected'])
        self.assertEqual('20B 전용', restored.state['histories']['20b'][0]['content'])
        qwen_prompt = self.models.calls[-1][1]
        self.assertNotIn('20B 전용', json.dumps(qwen_prompt, ensure_ascii=False))

    def test_clear_only_resets_current_model(self):
        self.submit(1, 'old')
        self.submit(2, '/qwen')
        self.submit(3, 'new')
        self.submit(4, '/clear')
        self.assertEqual([], self.bridge.state['histories']['qwen'])
        self.assertEqual(2, len(self.bridge.state['histories']['20b']))

    def test_failed_inference_does_not_pollute_history(self):
        self.models.fail = True
        self.submit(1, 'fail')
        self.assertEqual({}, self.bridge.state['histories'])
        self.assertIn('응답 실패', self.telegram.sent[0])

    def test_outbox_recovers_without_regenerating_reply(self):
        self.bridge.handle(self.update(1, 'hello'))
        self.telegram.fail = True
        with self.assertRaises(lb.BridgeError):
            self.bridge.flush()
        restored = lb.Bridge(self.config, self.telegram, self.models)
        self.assertEqual(2, restored.state['offset'])
        self.telegram.fail = False
        restored.flush()
        self.assertEqual(1, len(self.models.calls))
        self.assertEqual(1, len(self.telegram.sent))
        self.assertEqual([], restored.state['outbox'])

    def test_unconfirmed_delivery_is_not_removed_from_outbox(self):
        self.bridge.handle(self.update(1, 'hello'))
        with patch.object(self.telegram, 'call', return_value={}):
            with self.assertRaises(lb.BridgeError):
                self.bridge.flush()
        self.assertEqual(1, len(self.bridge.state['outbox']))

    def test_duplicate_update_is_not_processed_again(self):
        self.submit(10, 'hello')
        self.bridge.handle(self.update(10, 'hello'))
        self.assertEqual(1, len(self.models.calls))

    def test_pending_reply_must_be_flushed_before_next_update(self):
        self.bridge.handle(self.update(1, 'hello'))
        with self.assertRaises(lb.BridgeError):
            self.bridge.handle(self.update(2, 'next'))

    def test_history_bound_preserves_complete_turns(self):
        for index in range(8):
            self.submit(index, str(index))
        self.assertEqual(['user', 'assistant'] * 2,
                         [x['role'] for x in self.bridge.state['histories']['20b']])
        self.assertEqual('6', self.bridge.state['histories']['20b'][0]['content'])

    def test_state_file_is_private(self):
        self.submit(1, '/status')
        self.assertEqual(0o600, Path(self.config['state_file']).stat().st_mode & 0o777)

    def test_bot_command_suffix(self):
        self.submit(1, '/qwen@example_bot')
        self.assertEqual('qwen', self.bridge.state['selected'])

    def test_unsupported_media_gets_explanation(self):
        self.submit(1, '')
        self.assertIn('텍스트', self.telegram.sent[0])
        self.assertEqual([], self.models.calls)

    def test_chunks_roundtrip_emoji_within_utf16_limit(self):
        text = '가😀' * 4000
        parts = lb.split_message(text)
        self.assertEqual(text, ''.join(parts))
        self.assertTrue(all(len(p.encode('utf-16-le')) // 2 <= 3800 for p in parts))

    def test_remote_backend_rejected(self):
        config = copy.deepcopy(self.config)
        config['profiles']['qwen']['base_url'] = 'https://example.com'
        path = Path(self.tmp.name) / 'config.json'
        path.write_text(json.dumps(config))
        with self.assertRaises(lb.BridgeError):
            lb.read_config(path)

    def test_request_errors_do_not_leak_token_url(self):
        token = '123:example_secret'
        with patch.object(lb.urllib.request, 'urlopen', side_effect=urllib.error.URLError(token)):
            with self.assertRaises(lb.BridgeError) as caught:
                lb.json_request('https://api.telegram.org/bot' + token + '/getMe')
        self.assertNotIn(token, str(caught.exception))

    def test_model_release_precedes_loading_other_model(self):
        models = lb.LocalModels(self.config)
        events = []
        with patch.object(models, 'release', side_effect=lambda p: events.append(('release', p['model']))), \
             patch.object(models, 'ensure_lmstudio', side_effect=lambda p: events.append(('load', p['model']))), \
             patch.object(lb, 'json_request', return_value={'choices': [{'message': {'content': 'OK'}}]}):
            self.assertEqual('OK', models.chat('qwen', []))
        self.assertEqual([('release', 'test-20b'), ('load', 'test-qwen')], events)


if __name__ == '__main__':
    unittest.main()
