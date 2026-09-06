import builtins
import importlib.util
import io
import json
from contextlib import redirect_stdout
from pathlib import Path
import plistlib
import tempfile
import threading
import time
import unittest
from unittest.mock import patch


ROOT = Path(__file__).parents[1]
bridge_spec = importlib.util.spec_from_file_location(
    'local_bridge_acceptance', ROOT / 'local_bridge.py')
bridge = importlib.util.module_from_spec(bridge_spec)
bridge_spec.loader.exec_module(bridge)
terminal_spec = importlib.util.spec_from_file_location(
    'terminal_chat_acceptance', ROOT / 'terminal_chat.py')
terminal = importlib.util.module_from_spec(terminal_spec)
terminal_spec.loader.exec_module(terminal)
installer_spec = importlib.util.spec_from_file_location(
    'install_macos_acceptance', ROOT / 'install_macos.py')
installer = importlib.util.module_from_spec(installer_spec)
installer_spec.loader.exec_module(installer)


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


class SharedChatAcceptanceTests(unittest.TestCase):
    def test_reconnecting_terminal_replays_queued_telegram_question_before_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                'owner_id': 42,
                'default_profile': 'qwen',
                'fixed_profile': 'qwen',
                'history_turns': 3,
                'max_tokens': 64,
                'unload_other_profiles': True,
                'state_file': str(root / 'state.json'),
                'session_db': str(root / 'sessions.sqlite3'),
                'profiles': {
                    'qwen': {
                        'provider': 'lmstudio',
                        'label': 'Qwen',
                        'model': 'qwen',
                        'base_url': 'http://127.0.0.1:1234',
                    },
                },
            }
            config_path = root / 'config.json'
            config_path.write_text(json.dumps(config))
            app = bridge.Bridge(config, Telegram(), Models(), bridge_id='qwen-bot')
            app.store.enqueue(
                'qwen', 'telegram', 'qwen-bot:1', 'chat', 'reconnect question')

            waiting_for_input = threading.Event()
            release_input = threading.Event()
            errors = []
            output = io.StringIO()

            def blocked_input(_prompt):
                waiting_for_input.set()
                release_input.wait(3)
                raise EOFError

            def run_terminal():
                try:
                    with patch.object(
                            terminal.sys, 'argv',
                            ['terminal_chat.py', '--config', str(config_path)]), \
                            patch.object(builtins, 'input', blocked_input), \
                            redirect_stdout(output):
                        terminal.main()
                except Exception as error:  # surfaced below with the captured output
                    errors.append(error)

            client = threading.Thread(target=run_terminal)
            client.start()
            self.assertTrue(waiting_for_input.wait(2), 'terminal did not reach its prompt')
            self.assertTrue(app.process_pending())

            deadline = time.monotonic() + 2
            while 'answer:reconnect question' not in output.getvalue() and time.monotonic() < deadline:
                time.sleep(0.02)
            release_input.set()
            client.join(3)

            self.assertFalse(client.is_alive())
            self.assertEqual([], errors)
            rendered = output.getvalue()
            question = rendered.find('Telegram › reconnect question')
            answer = rendered.find('Qwen › answer:reconnect question')
            self.assertNotEqual(-1, question, rendered)
            self.assertNotEqual(-1, answer, rendered)
            self.assertLess(question, answer, rendered)

    def test_simultaneous_terminal_and_telegram_inputs_follow_commit_order_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            config = {
                'owner_id': 42,
                'default_profile': 'qwen',
                'fixed_profile': 'qwen',
                'history_turns': 3,
                'max_tokens': 64,
                'unload_other_profiles': True,
                'state_file': str(root / 'state.json'),
                'session_db': str(root / 'sessions.sqlite3'),
                'profiles': {
                    'qwen': {
                        'provider': 'lmstudio',
                        'label': 'Qwen',
                        'model': 'qwen',
                        'base_url': 'http://127.0.0.1:1234',
                    },
                },
            }
            config_path = root / 'config.json'
            config_path.write_text(json.dumps(config))
            telegram, models = Telegram(), Models()
            app = bridge.Bridge(config, telegram, models, bridge_id='qwen-bot')
            start = threading.Barrier(2)
            replies = iter(['terminal question', '/exit'])
            errors = []

            def terminal_input(_prompt):
                value = next(replies)
                if value == 'terminal question':
                    start.wait(2)
                return value

            def run_terminal():
                try:
                    with patch.object(
                            terminal.sys, 'argv',
                            ['terminal_chat.py', '--config', str(config_path)]), \
                            patch.object(builtins, 'input', terminal_input), \
                            redirect_stdout(io.StringIO()):
                        terminal.main()
                except Exception as error:
                    errors.append(error)

            client = threading.Thread(target=run_terminal)
            client.start()
            start.wait(2)
            app.handle({
                'update_id': 1,
                'message': {
                    'text': 'telegram question',
                    'from': {'id': 42},
                    'chat': {'id': 42, 'type': 'private'},
                },
            })
            client.join(3)
            self.assertFalse(client.is_alive())
            self.assertEqual([], errors)

            app.flush()
            while app.process_pending():
                app.flush()

            prompts = [call[1][-1]['content'] for call in models.calls]
            self.assertCountEqual(
                ['terminal question', 'telegram question'], prompts)
            self.assertEqual(2, len(prompts))
            history = app.store.history('qwen')
            self.assertEqual(prompts, [history[0]['content'], history[2]['content']])
            self.assertEqual(
                ['answer:' + prompts[0], 'answer:' + prompts[1]],
                [history[1]['content'], history[3]['content']])
            second_context = models.calls[1][1]
            self.assertEqual(prompts[0], second_context[1]['content'])
            self.assertEqual('answer:' + prompts[0], second_context[2]['content'])
            self.assertEqual(3, len(telegram.sent))
            self.assertEqual(1, telegram.sent.count('[Terminal]\nterminal question'))
            self.assertEqual(
                1, telegram.sent.count('[Qwen]\nanswer:terminal question'))
            self.assertEqual(
                1, telegram.sent.count('[Qwen]\nanswer:telegram question'))

    def test_installed_service_still_launches_bridge_after_copying_shared_clients(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / 'qwen.json'
            config.write_text('{}')
            (home / 'Library' / 'LaunchAgents').mkdir(parents=True)
            with patch.object(installer.Path, 'home', return_value=home), \
                    patch.object(installer, 'read_config'), \
                    patch.object(installer.sys, 'platform', 'darwin'), \
                    patch.object(
                        installer.sys, 'argv',
                        ['install_macos.py', '--config', str(config),
                         '--instance', 'qwen']), \
                    patch.object(installer.subprocess, 'run'), \
                    redirect_stdout(io.StringIO()):
                installer.main()

            plist_path = (
                home / 'Library' / 'LaunchAgents' /
                'com.local-telegram-bridge-qwen.plist')
            service = plistlib.loads(plist_path.read_bytes())
            expected = home / '.local/share/local-telegram-bridge/local_bridge.py'
            self.assertEqual(str(expected), service['ProgramArguments'][1])


if __name__ == '__main__':
    unittest.main()
