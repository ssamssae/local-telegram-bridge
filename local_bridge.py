#!/usr/bin/env python3
"""Owner-only Telegram chat with local Ollama and LM Studio models (Python 3.9+)."""
from __future__ import annotations

import argparse
import copy
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


class BridgeError(Exception):
    """A diagnostic that is safe to print without exposing credentials or prompts."""


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
    config.setdefault('history_turns', 6)
    config.setdefault('max_tokens', 1024)
    config.setdefault('unload_other_profiles', False)
    return config


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
            raise BridgeError('Model returned no answer; try a shorter question or /new')
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
    def __init__(self, config, telegram, models):
        self.config, self.telegram, self.models = config, telegram, models
        self.state_path = Path(config['state_file']).expanduser()
        if self.state_path.exists():
            self.state = json.loads(self.state_path.read_text())
        else:
            self.state = {'offset': 0, 'histories': {}, 'outbox': []}
        self.state.setdefault('selected', config['default_profile'])
        self.state.setdefault('histories', {})
        self.state.setdefault('outbox', [])
        if self.state['selected'] not in config['profiles']:
            raise BridgeError('Saved profile missing from config; update the private state')

    def save(self):
        atomic_json(self.state_path, self.state)

    def flush(self):
        while self.state['outbox']:
            item = self.state['outbox'][0]
            result = self.telegram.call('sendMessage', chat_id=self.config['owner_id'],
                                        text=item['text'])
            if not isinstance(result, dict) or not result.get('message_id'):
                raise BridgeError('Telegram delivery was not confirmed')
            self.state['last_delivery'] = {'message_id': result['message_id'],
                                           'update_id': item['update_id'], 'at': time.time()}
            self.state['outbox'].pop(0)
            self.save()

    def help(self, selected):
        rows = ['로컬 AI 채팅 · 현재: ' + self.config['profiles'][selected]['label']]
        rows += ['/' + name + ' → ' + p['label'] for name, p in self.config['profiles'].items()]
        rows += ['/status 현재 모델 · /new 현재 모델의 새 대화 · /help 도움말',
                 '모델 연산은 이 컴퓨터에서 실행합니다. 메시지는 Telegram을 거칩니다.']
        return '\n'.join(rows)

    def handle(self, update):
        update_id = update.get('update_id')
        if not isinstance(update_id, int) or update_id < self.state.get('offset', 0):
            return
        if self.state['outbox']:
            raise BridgeError('Flush pending replies before accepting another update')
        following = copy.deepcopy(self.state)
        following['offset'] = update_id + 1
        message = update.get('message') or {}
        chat, sender = message.get('chat') or {}, message.get('from') or {}
        owner = self.config['owner_id']
        if chat.get('type') != 'private' or chat.get('id') != owner or sender.get('id') != owner:
            self.state = following
            self.save()
            return
        text = (message.get('text') or '').strip()
        selected = following['selected']
        command, _, argument = text.partition(' ')
        command = command.split('@', 1)[0].lower()
        if not text:
            reply = '이 연결은 텍스트 채팅용입니다. 질문을 글로 보내주세요.'
        elif command in ('/start', '/help'):
            reply = self.help(selected)
        elif command in ('/new', '/clear'):
            following['histories'][selected] = []
            reply = self.config['profiles'][selected]['label'] + ' · 새 대화를 시작합니다.'
        elif command in ('/status', '/models'):
            count = len(following['histories'].get(selected, [])) // 2
            reply = self.help(selected) + '\n저장된 대화: ' + str(count) + '턴'
        elif command.startswith('/') and command[1:] in self.config['profiles']:
            selected = command[1:]
            following['selected'] = selected
            reply = self.config['profiles'][selected]['label'] + '로 전환했습니다. 질문을 보내주세요.'
            if argument:
                reply += '\n이번 명령 뒤의 질문은 실행하지 않았습니다. 질문을 새 메시지로 보내주세요.'
        elif command.startswith('/'):
            reply = '알 수 없는 명령입니다. /help를 확인하세요.'
        else:
            history = following['histories'].get(selected, [])
            limit = max(1, int(self.config['history_turns'])) * 2
            prompt = self.config['profiles'][selected].get('system',
                '한국어로 명확하고 간결하게 답하세요. 파일 편집이나 명령 실행 도구는 없습니다.')
            messages = [{'role': 'system', 'content': prompt}] + history[-limit:] + [
                {'role': 'user', 'content': text}]
            try:
                with Typing(self.telegram, owner):
                    answer = self.models.chat(selected, messages)
                following['histories'][selected] = (history + [
                    {'role': 'user', 'content': text}, {'role': 'assistant', 'content': answer}])[-limit:]
                reply = '[' + self.config['profiles'][selected]['label'] + ']\n' + answer
            except (BridgeError, subprocess.SubprocessError) as error:
                safe = str(error) if isinstance(error, BridgeError) else 'Local application could not start'
                reply = '응답 실패: ' + safe + '\n이번 질문은 대화에 저장하지 않았습니다. 다시 보내거나 /new를 사용하세요.'
        following['outbox'] = [{'update_id': update_id, 'text': part} for part in split_message(reply)]
        self.state = following
        # Commit generated replies and offset together BEFORE attempting Telegram delivery.
        self.save()

    def run(self):
        retry = 1
        while True:
            try:
                self.flush()
                updates = self.telegram.call('getUpdates', offset=self.state.get('offset', 0),
                                               timeout=25, allowed_updates=['message'])
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
        commands = [{'command': name, 'description': p['label'][:256]} for name, p in config['profiles'].items()]
        commands += [{'command': 'status', 'description': '현재 모델과 대화 상태'},
                     {'command': 'new', 'description': '현재 모델의 새 대화'},
                     {'command': 'help', 'description': '사용 방법'}]
        telegram.call('setMyCommands', commands=commands)
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
        Bridge(config, telegram, LocalModels(config)).run()


if __name__ == '__main__':
    try:
        main()
    except KeyboardInterrupt:
        pass
    except (BridgeError, OSError, ValueError) as error:
        print('bridge: ' + (str(error) if isinstance(error, BridgeError) else type(error).__name__), file=sys.stderr)
        sys.exit(1)
