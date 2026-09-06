#!/usr/bin/env python3
"""Interactive terminal client for a local-telegram-bridge shared session."""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import readline
import sys
import threading
import time
import uuid

from local_bridge import BridgeError, read_config
from session_store import SessionStore, SessionStoreError


class Console:
    """Render asynchronous events without discarding the current readline buffer."""

    def __init__(self, prompt, stream=None, line_editor=None):
        self.prompt = prompt
        self.stream = stream or sys.stdout
        self.line_editor = line_editor or readline
        self.lock = threading.Lock()
        self.input_active = False

    def begin_input(self):
        with self.lock:
            self.input_active = True

    def end_input(self):
        with self.lock:
            self.input_active = False

    def event(self, text):
        with self.lock:
            buffer = self.line_editor.get_line_buffer() if self.input_active else ''
            if self.input_active:
                self.stream.write('\r\x1b[2K')
            self.stream.write(text + '\n')
            if self.input_active:
                self.stream.write(self.prompt + buffer)
            self.stream.flush()


def event_text(event, label):
    if event['kind'] == 'user':
        origin = 'Telegram' if event['source'] == 'telegram' else 'Terminal'
        return origin + ' › ' + event['content']
    if event['kind'] == 'assistant':
        return label + ' › ' + event['content']
    if event['kind'] == 'clear':
        return '— ' + event['content'] + ' —'
    return '오류 › ' + event['content']


def choose_profile(config, requested):
    fixed = config.get('fixed_profile')
    profile = requested or fixed or config['default_profile']
    if fixed and profile != fixed:
        raise BridgeError('This config is fixed to profile ' + fixed)
    if profile not in config['profiles']:
        raise BridgeError('Unknown profile ' + profile)
    return profile


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True, help='Private bridge JSON configuration')
    parser.add_argument('--profile', help='Configured profile, for example 20b or qwen')
    parser.add_argument('--check', action='store_true', help='Validate config and shared database only')
    args = parser.parse_args()
    config = read_config(args.config)
    profile = choose_profile(config, args.profile)
    store = SessionStore(config['session_db'])
    label = config['profiles'][profile]['label']
    if args.check:
        print('OK: ' + label + ' · ' + str(store.path))
        return

    client_id = str(os.getuid()) + '-' + uuid.uuid4().hex
    prompt = profile + ' › '
    console = Console(prompt)
    print(label + ' · shared Telegram/terminal session')
    print('/clear 새 대화 · /exit 종료 · 모델 전환 없음')
    history, pending, cursor = store.terminal_snapshot(profile)
    if history:
        print('저장된 대화 ' + str(len(history) // 2) + '턴:')
        for row in history:
            who = '나' if row['role'] == 'user' else label
            print(who + ' › ' + row['content'])
    for event in pending:
        print(event_text(event, label))
    stop = threading.Event()

    def watch():
        nonlocal cursor
        while not stop.wait(0.2):
            for event in store.events_since(profile, cursor):
                cursor = event['id']
                if (event['kind'] == 'user' and event['source'] == 'terminal' and
                        (event.get('source_key') or '').startswith(client_id + ':')):
                    continue
                console.event(event_text(event, label))

    watcher = threading.Thread(target=watch, daemon=True)
    watcher.start()
    try:
        while True:
            console.begin_input()
            try:
                text = input(prompt).strip()
            except (EOFError, KeyboardInterrupt):
                print()
                return
            finally:
                console.end_input()
            if not text:
                continue
            if text in ('/exit', '/quit', '/bye'):
                return
            if text == '/help':
                console.event('/clear 새 대화 · /exit 종료 · 입력은 bridge의 영속 큐에 저장됩니다.')
                continue
            if text.startswith('/') and text not in ('/clear', '/new'):
                console.event('이 터미널에서는 /clear와 /exit만 사용할 수 있습니다.')
                continue
            kind = 'clear' if text in ('/clear', '/new') else 'chat'
            content = '' if kind == 'clear' else text
            store.enqueue(profile, 'terminal', client_id + ':' + uuid.uuid4().hex,
                          kind, content)
    finally:
        stop.set()
        watcher.join(1)


if __name__ == '__main__':
    try:
        main()
    except (BridgeError, SessionStoreError, OSError, ValueError) as error:
        print('terminal_chat: ' + str(error), file=sys.stderr)
        sys.exit(1)
