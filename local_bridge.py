#!/usr/bin/env python3
"""Owner-only Telegram chat with local Ollama and LM Studio models (Python 3.9+)."""
from __future__ import annotations

import argparse
import copy
from contextlib import contextmanager, ExitStack
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request

from session_store import SessionStore, SessionStoreError


class BridgeError(Exception):
    """A diagnostic that is safe to print without exposing credentials or prompts."""


@contextmanager
def inference_lock(path):
    """Serialize cooperating clients across model unload, load, and inference."""
    if not path:
        yield
        return
    target = Path(path).expanduser()
    target.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(target, os.O_WRONLY | os.O_CREAT, 0o600)
    with os.fdopen(fd, 'w') as lock:
        os.fchmod(lock.fileno(), 0o600)
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.fchmod(fd, 0o600)
    with os.fdopen(fd, 'w') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    tmp.replace(path)


def json_request(url, payload=None, timeout=30, headers=None):
    data = None if payload is None else json.dumps(payload, ensure_ascii=False).encode()
    req = urllib.request.Request(url, data=data, headers={
        'Content-Type': 'application/json', **(headers or {})})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as error:
        raise BridgeError('HTTP ' + str(error.code)) from None
    except (OSError, ValueError):
        # urllib exception strings can contain the full Telegram token-bearing URL.
        raise BridgeError('Connection failed or invalid JSON response') from None


def mask_secrets(text):
    """Hide assignment/JSON secret values. Keep names like token.json."""
    raw = text or ""
    raw = re.sub(r"\b\d{8,12}:[A-Za-z0-9_-]{20,}\b", "<redacted-token>", raw)
    raw = re.sub(r"\bsk-[A-Za-z0-9][-A-Za-z0-9]{8,}\b", "<redacted-key>", raw)
    raw = re.sub(
        r'(?i)("(?:api[_-]?key|password|passwd|secret|token|otp|비밀번호)"\s*:\s*")([^"]*)(")',
        r"\1<redacted>\3",
        raw,
    )
    raw = re.sub(
        r"(?i)(?:api[_-]?key|passwd|password|secret|(?<![A-Za-z0-9_.-])token|otp)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|\S+)",
        lambda m: re.sub(r"[:=]\s*.*$", "=<redacted>", m.group(0)),
        raw,
    )
    raw = re.sub(
        r"(비밀번호|비번|인증번호|인증\s*코드)\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\n]+)",
        lambda m: f"{m.group(1)}=<redacted>",
        raw,
    )
    return raw


def split_message(text, limit=3800):
    """Stay below Telegram's limit even when text contains UTF-16 surrogate pairs."""
    parts, current, units = [], [], 0
    for char in text:
        width = len(char.encode('utf-16-le')) // 2
        if units + width > limit:
            parts.append(''.join(current))
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        parts.append(''.join(current))
    return parts


def read_config(path):
    config = json.loads(Path(path).expanduser().read_text())
    if not isinstance(config.get('owner_id'), int) or config['owner_id'] <= 0:
        raise BridgeError('Set a positive integer owner_id in the private config')
    profiles = config.get('profiles', {})
    if not profiles or config.get('default_profile') not in profiles:
        raise BridgeError('Set profiles and default_profile')
    if 'fixed_profile' in config and config['fixed_profile'] not in profiles:
        raise BridgeError('fixed_profile must name a configured profile')
    for name, profile in profiles.items():
        if not re.fullmatch('[a-z0-9_]{1,32}', name):
            raise BridgeError('Profile names must be valid Telegram commands')
        if profile.get('provider') not in ('ollama', 'lmstudio'):
            raise BridgeError('Supported providers: ollama, lmstudio')
        parsed = urllib.parse.urlparse(profile.get('base_url', ''))
        if parsed.scheme != 'http' or parsed.hostname not in ('127.0.0.1', 'localhost', '::1'):
            raise BridgeError('Backend URLs must use HTTP on localhost')
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise BridgeError('Backend credentials must not appear in the URL')
        if not isinstance(profile.get('model'), str) or not profile['model']:
            raise BridgeError('Each profile needs a model identifier')
        profile.setdefault('label', profile['model'])
    config.setdefault('state_file', '~/.local/state/local-telegram-bridge/state.json')
    state_parent = Path(config['state_file']).expanduser().parent
    config.setdefault('session_db', str(state_parent / 'sessions.sqlite3'))
    if not isinstance(config['session_db'], str) or not config['session_db'].strip():
        raise BridgeError('session_db must be a private SQLite file path')
    if Path(config['session_db']).expanduser().resolve() == Path(config['state_file']).expanduser().resolve():
        raise BridgeError('session_db and state_file must use different paths')
    config.setdefault('history_turns', 6)
    config.setdefault('max_tokens', 1024)
    config.setdefault('unload_other_profiles', False)
    return config


def bot_commands(config):
    commands = [{'command': 'clear', 'description': '새 대화 시작'},
                {'command': 'status', 'description': '모델과 대화 상태'},
                {'command': 'help', 'description': '사용 방법'}]
    if not config.get('fixed_profile'):
        commands.insert(0, {'command': 'model', 'description': '버튼으로 모델 선택'})
    return commands


def read_token(config):
    token = os.environ.get(config.get('token_env', 'TELEGRAM_BOT_TOKEN'), '').strip()
    if not token and config.get('token_file'):
        raw = Path(config['token_file']).expanduser().read_text().strip()
        if raw.startswith('{'):
            data = json.loads(raw)
            token = data.get('token') or data.get('api_key') or ''
        else:
            token = raw
    if not re.fullmatch(r'\d+:[A-Za-z0-9_-]+', token):
        raise BridgeError('Provide a Telegram bot token using token_env or token_file')
    return token


class Telegram:
    def __init__(self, token):
        self.token = token

    def call(self, method, **payload):
        data = json_request('https://api.telegram.org/bot' + self.token + '/' + method,
                            payload, timeout=40)
        if not data.get('ok'):
            raise BridgeError('Telegram rejected ' + method)
        return data.get('result')


class LocalModels:
    def __init__(self, config):
        self.config = config

    def lms(self, profile, *args, timeout=180):
        binary = os.path.expanduser(profile.get('lms_bin', 'lms'))
        binary = shutil.which(binary) or binary
        try:
            result = subprocess.run([binary, *args], capture_output=True, text=True,
                                    timeout=timeout)
        except (OSError, subprocess.SubprocessError):
            raise BridgeError('LM Studio CLI unavailable or timed out') from None
        if result.returncode:
            raise BridgeError('LM Studio command failed; check lms and memory settings')
        return result.stdout

    def ensure_lmstudio(self, profile):
        url = profile['base_url'].rstrip('/')
        try:
            json_request(url + '/v1/models', timeout=3)
        except BridgeError:
            if sys.platform == 'darwin' and profile.get('start_app', False):
                subprocess.run(['/usr/bin/open', '-g', '-a', 'LM Studio'],
                               capture_output=True, check=True)
            port = str(urllib.parse.urlparse(url).port or 1234)
            for attempt in range(10):
                try:
                    self.lms(profile, 'server', 'start', '--port', port,
                             '--bind', '127.0.0.1', timeout=5)
                    break
                except BridgeError:
                    if attempt == 9:
                        raise
                    time.sleep(1)
        loaded = json.loads(self.lms(profile, 'ps', '--json', timeout=15))
        if not any(row.get('identifier') == profile['model'] for row in loaded):
            self.lms(profile, 'load', profile.get('load_model', profile['model']),
                     '--identifier', profile['model'], '--context-length',
                     str(profile.get('context_length', 4096)), '--parallel', '1',
                     '--no-speculative-draft-mtp', '--ttl',
                     str(profile.get('ttl_seconds', 60)), '-y')

    def release(self, profile):
        url = profile['base_url'].rstrip('/')
        try:
            if profile['provider'] == 'ollama':
                loaded = json_request(url + '/api/ps', timeout=3).get('models', [])
                for row in loaded:
                    if row.get('name', '').removesuffix(':latest') == profile['model'].removesuffix(':latest'):
                        json_request(url + '/api/generate', {
                            'model': profile['model'], 'keep_alive': 0}, timeout=30)
            else:
                # An offline peer already holds no model memory.
                json_request(url + '/v1/models', timeout=3)
                loaded = json.loads(self.lms(profile, 'ps', '--json', timeout=15))
                if any(row.get('identifier') == profile['model'] for row in loaded):
                    self.lms(profile, 'unload', profile['model'], timeout=30)
        except BridgeError:
            # Refuse to load a second large model if a reachable peer failed to unload.
            try:
                json_request(url + ('/api/ps' if profile['provider'] == 'ollama' else '/v1/models'), timeout=3)
            except BridgeError:
                return
            raise

    def chat(self, name, messages):
        with inference_lock(self.config.get('inference_lock_file')):
            return self._chat(name, messages)

    def _chat(self, name, messages):
        profile = self.config['profiles'][name]
        if self.config['unload_other_profiles']:
            for other_name, other in self.config['profiles'].items():
                if other_name != name:
                    self.release(other)
        url = profile['base_url'].rstrip('/')
        if profile['provider'] == 'lmstudio':
            self.ensure_lmstudio(profile)
            data = json_request(url + '/v1/chat/completions', {
                'model': profile['model'], 'messages': messages, 'stream': False,
                'max_tokens': self.config['max_tokens'], 'temperature': 0.2}, timeout=300)
            choices = data.get('choices') or []
            text = (choices[0].get('message', {}).get('content') or '') if choices else ''
        else:
            data = json_request(url + '/api/chat', {
                'model': profile['model'], 'messages': messages, 'stream': False,
                'keep_alive': str(profile.get('ttl_seconds', 60)) + 's',
                'options': {'num_ctx': profile.get('context_length', 4096),
                            'num_predict': self.config['max_tokens'], 'temperature': 0.2}}, timeout=300)
            text = (data.get('message') or {}).get('content') or ''
        if not text.strip():
            raise BridgeError('Model returned no answer; try a shorter question or /clear')
        return text.strip()


class Typing:
    def __init__(self, telegram, owner):
        self.telegram, self.owner = telegram, owner
        self.stop = threading.Event()

    def __enter__(self):
        def run():
            while not self.stop.is_set():
                try:
                    self.telegram.call('sendChatAction', chat_id=self.owner, action='typing')
                except BridgeError:
                    pass
                self.stop.wait(4)
        self.thread = threading.Thread(target=run, daemon=True)
        self.thread.start()
        return self

    def __exit__(self, *args):
        self.stop.set()


class Bridge:
    def __init__(self, config, telegram, models, bridge_id=None, store=None):
        self.config, self.telegram, self.models = config, telegram, models
        self.state_path = Path(config['state_file']).expanduser()
        config.setdefault('session_db', str(self.state_path.parent / 'sessions.sqlite3'))
        self.store = store or SessionStore(config['session_db'])
        fixed = config.get('fixed_profile')
        self.profiles = [fixed] if fixed else list(config['profiles'])
        self.bridge_id = bridge_id or hashlib.sha256(
            str(self.state_path.resolve()).encode()).hexdigest()[:20]
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
        else:
            self.state = {'offset': 0, 'histories': {}, 'outbox': []}
        self.state.setdefault('selected', config['default_profile'])
        self.state.setdefault('histories', {})
        self.state.setdefault('outbox', [])
        if config.get('fixed_profile'):
            # A previous model selection must not override this bot's binding.
            self.state['selected'] = config['fixed_profile']
        if self.state['selected'] not in config['profiles']:
            raise BridgeError('Saved profile missing from config; update the private state')
        for name in self.profiles:
            rows = self.state['histories'].get(name, [])
            if (rows and not self.store.history(name)
                    and not self.store.was_cleared(name)):
                raise BridgeError('Legacy JSON history needs migrate_history.py before bridge startup')
        self._sync_history_state()

    def save(self):
        atomic_json(self.state_path, self.state)

    def _sync_history_state(self):
        # Compatibility snapshot only; SQLite is the shared source of truth.
        self.state['histories'] = {
            name: self.store.history(name) for name in self.profiles
        }

    def flush(self):
        while self.state['outbox']:
            item = self.state['outbox'][0]
            extra = {'reply_markup': item['reply_markup']} if 'reply_markup' in item else {}
            result = self.telegram.call('sendMessage', chat_id=self.config['owner_id'],
                                        text=item['text'], **extra)
            if not isinstance(result, dict) or not result.get('message_id'):
                raise BridgeError('Telegram delivery was not confirmed')
            self.state['last_delivery'] = {'message_id': result['message_id'],
                                           'update_id': item['update_id'], 'at': time.time()}
            self.state['outbox'].pop(0)
            self.save()
        while True:
            item = self.store.pending_outbox(self.profiles)
            if not item:
                break
            result = self.telegram.call('sendMessage', chat_id=self.config['owner_id'],
                                        text=item['text'])
            if not isinstance(result, dict) or not result.get('message_id'):
                raise BridgeError('Telegram delivery was not confirmed')
            self.store.mark_sent(item['id'], result['message_id'])
            self.state['last_delivery'] = {'message_id': result['message_id'],
                                           'request_id': item['request_id'], 'at': time.time()}
            self.save()

    def help(self, selected):
        rows = ['로컬 AI 채팅 · 현재: ' + self.config['profiles'][selected]['label']]
        commands = '/clear 새 대화 · /status 현재 모델 · /help 도움말'
        if not self.config.get('fixed_profile'):
            commands = '/model 모델 선택 · ' + commands
        rows += [commands,
                 '모델 연산은 이 컴퓨터에서 실행합니다. 메시지는 Telegram을 거칩니다.']
        return '\n'.join(rows)

    def model_picker(self, selected):
        keyboard = {'inline_keyboard': [
            [{'text': ('✅ ' if name == selected else '') + profile['label'],
              'callback_data': 'model:' + name}]
            for name, profile in self.config['profiles'].items()]}
        text = '모델을 선택하세요.\n현재: ' + self.config['profiles'][selected]['label']
        return text, keyboard

    def queue_reply(self, following, update_id, reply, markup=None):
        following['outbox'] = [{'update_id': update_id, 'text': part} for part in split_message(reply)]
        if markup and following['outbox']:
            following['outbox'][-1]['reply_markup'] = markup
        self.state = following
        # Commit generated replies and offset together BEFORE Telegram delivery.
        self.save()

    def _request_deliveries(self, request, reply):
        rows = []
        if request['source'] == 'terminal':
            shown = '/clear' if request['kind'] == 'clear' else request['content']
            rows.extend(split_message('[Terminal]\n' + mask_secrets(shown)))
        rows.extend(split_message(reply))
        return rows

    def process_pending(self):
        request = self.store.claim(self.profiles)
        if not request:
            return False
        selected = request['profile']
        label = self.config['profiles'][selected]['label']
        if request['kind'] == 'clear':
            reply = label + ' · 새 대화를 시작합니다.'
            self.store.complete_clear(request, reply,
                                      self._request_deliveries(request, reply))
        else:
            history = self.store.history(selected, self.config['history_turns'])
            prompt = self.config['profiles'][selected].get('system',
                '한국어로 명확하고 간결하게 답하세요. 파일 편집이나 명령 실행 도구는 없습니다.')
            messages = [{'role': 'system', 'content': prompt}] + history + [
                {'role': 'user', 'content': request['content']}]
            try:
                if request['source'] == 'telegram':
                    with Typing(self.telegram, self.config['owner_id']):
                        answer = self.models.chat(selected, messages)
                else:
                    answer = self.models.chat(selected, messages)
                reply = '[' + label + ']\n' + answer
                self.store.complete_chat(request, answer, self.config['history_turns'],
                                         self._request_deliveries(request, reply))
            except (BridgeError, subprocess.SubprocessError) as error:
                safe = str(error) if isinstance(error, BridgeError) else 'Local application could not start'
                reply = ('응답 실패: ' + safe +
                         '\n이번 질문은 대화에 저장하지 않았습니다. 다시 보내거나 /clear를 사용하세요.')
                self.store.fail(request, reply, self._request_deliveries(request, reply))
        self._sync_history_state()
        self.save()
        return True

    def handle(self, update):
        update_id = update.get('update_id')
        if not isinstance(update_id, int) or update_id < self.state.get('offset', 0):
            return
        if self.state['outbox'] or self.store.pending_outbox(self.profiles):
            raise BridgeError('Flush pending replies before accepting another update')
        following = copy.deepcopy(self.state)
        following['offset'] = update_id + 1
        callback = update.get('callback_query')
        message = (callback.get('message') if callback else update.get('message')) or {}
        chat = message.get('chat') or {}
        sender = (callback.get('from') if callback else message.get('from')) or {}
        owner = self.config['owner_id']
        if chat.get('type') != 'private' or chat.get('id') != owner or sender.get('id') != owner:
            self.state = following
            self.save()
            return
        selected = following['selected']
        fixed = self.config.get('fixed_profile')
        if callback:
            data = callback.get('data')
            name = data[6:] if isinstance(data, str) and data.startswith('model:') else ''
            if fixed:
                reply = self.config['profiles'][selected]['label'] + ' 전용 봇입니다. 질문을 보내주세요.'
                notice = '이 봇에서는 모델을 전환하지 않습니다.'
            elif name in self.config['profiles']:
                selected = name
                following['selected'] = name
                reply = self.config['profiles'][name]['label'] + '로 전환했습니다. 질문을 보내주세요.'
                notice = '모델을 선택했습니다.'
            else:
                reply = '사용할 수 없는 모델입니다. 아래에서 다시 선택하세요.'
                notice = '모델 목록을 다시 확인하세요.'
            markup = None if fixed else self.model_picker(selected)[1]
            self.queue_reply(following, update_id, reply, markup)
            if callback.get('id'):
                try:
                    self.telegram.call('answerCallbackQuery', callback_query_id=callback['id'], text=notice)
                except BridgeError:
                    # Expired callback acknowledgements must not block the saved reply.
                    pass
            return
        text = (message.get('text') or '').strip()
        markup = None
        command, _, argument = text.partition(' ')
        command = command.split('@', 1)[0].lower()
        if not text:
            reply = '이 연결은 텍스트 채팅용입니다. 질문을 글로 보내주세요.'
        elif command in ('/start', '/help'):
            reply = self.help(selected)
        elif command == '/clear':
            self.store.enqueue(selected, 'telegram', self.bridge_id + ':' + str(update_id),
                               'clear', '')
            self.state = following
            self.save()
            self.process_pending()
            return
        elif command == '/new':
            reply = '새 대화 명령이 /clear로 바뀌었습니다. /clear를 보내주세요.'
        elif not fixed and command in ('/model', '/models'):
            reply, markup = self.model_picker(selected)
        elif command == '/status':
            count = len(self.store.history(selected)) // 2
            reply = self.help(selected) + '\n저장된 대화: ' + str(count) + '턴'
        elif not fixed and command.startswith('/') and command[1:] in self.config['profiles']:
            selected = command[1:]
            following['selected'] = selected
            reply = self.config['profiles'][selected]['label'] + '로 전환했습니다. 질문을 보내주세요.'
            if argument:
                reply += '\n이번 명령 뒤의 질문은 실행하지 않았습니다. 질문을 새 메시지로 보내주세요.'
        elif command.startswith('/'):
            reply = '알 수 없는 명령입니다. /help를 확인하세요.'
        else:
            self.store.enqueue(selected, 'telegram', self.bridge_id + ':' + str(update_id),
                               'chat', text)
            self.state = following
            self.save()
            self.process_pending()
            return
        self.queue_reply(following, update_id, reply, markup)

    def run(self):
        retry = 1
        while True:
            try:
                self.flush()
                while self.process_pending():
                    self.flush()
                updates = self.telegram.call('getUpdates', offset=self.state.get('offset', 0),
                                               timeout=1, allowed_updates=['message', 'callback_query'])
                for update in updates or []:
                    self.handle(update)
                    self.flush()
                retry = 1
            except BridgeError as error:
                print('bridge: ' + str(error), flush=True)
                time.sleep(retry)
                retry = min(30, retry * 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Private JSON configuration file')
    parser.add_argument('--check', action='store_true', help='Validate config and Telegram identity; do not poll')
    parser.add_argument('--register-commands', action='store_true', help='Update this bot’s command menu')
    args = parser.parse_args()
    config = read_config(args.config)
    token = read_token(config)
    telegram = Telegram(token)
    identity = telegram.call('getMe')
    if args.check:
        print(json.dumps({'bot': identity['username'], 'profiles': list(config['profiles'])}))
        return
    if args.register_commands:
        telegram.call('setMyCommands', commands=bot_commands(config))
        print('Bot command menu updated')
        return
    lock_dir = Path.home() / '.local/state/local-telegram-bridge/locks'
    lock_dir.mkdir(parents=True, exist_ok=True)
    lock_path = lock_dir / (hashlib.sha256(token.encode()).hexdigest()[:20] + '.lock')
    with lock_path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BridgeError('Another bridge process already uses this bot') from None
        if telegram.call('getWebhookInfo').get('url'):
            raise BridgeError('This bot has a webhook; remove it deliberately before long polling')
        print('Local Telegram bridge started: @' + identity['username'], flush=True)
        bridge = Bridge(config, telegram, LocalModels(config),
                        bridge_id=hashlib.sha256(token.encode()).hexdigest()[:20])
        with ExitStack() as workers:
            for profile in bridge.profiles:
                workers.enter_context(bridge.store.worker_lock(profile))
            bridge.store.recover(bridge.profiles)
            bridge.run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (BridgeError, SessionStoreError, OSError, ValueError) as error:
        print('bridge: ' + (str(error) if isinstance(error, BridgeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
