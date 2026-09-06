#!/usr/bin/env python3
"""Safely seed shared sessions from legacy bridge and terminal JSON histories."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from local_bridge import BridgeError, read_config
from session_store import SessionStore, SessionStoreError


def validate_rows(value, source):
    if not isinstance(value, list):
        raise BridgeError(source + ': history must be a JSON list')
    rows = []
    for index, row in enumerate(value):
        if (not isinstance(row, dict) or row.get('role') not in ('user', 'assistant') or
                not isinstance(row.get('content'), str)):
            raise BridgeError(source + ': invalid row ' + str(index))
        rows.append({'role': row['role'], 'content': row['content']})
    if any(row['role'] != ('user' if index % 2 == 0 else 'assistant')
           for index, row in enumerate(rows)):
        raise BridgeError(source + ': history must contain complete user/assistant turns')
    return rows


def read_telegram_state(path, profiles):
    data = json.loads(path.read_text())
    histories = data.get('histories', {})
    if not isinstance(histories, dict):
        raise BridgeError(str(path) + ': histories must be an object')
    result = []
    for profile, value in histories.items():
        if profile in profiles and value:
            result.append({'profile': profile, 'kind': 'telegram', 'name': path.name,
                           'rows': validate_rows(value, str(path) + ':' + profile)})
    return result


def parse_terminal(value, profiles):
    profile, separator, raw_path = value.partition('=')
    if not separator or profile not in profiles:
        raise BridgeError('--terminal-history must be PROFILE=PATH for a configured profile')
    path = Path(raw_path).expanduser().resolve()
    return {'profile': profile, 'kind': 'terminal', 'name': path.name,
            'rows': validate_rows(json.loads(path.read_text()), str(path))}


def select_active(profile, candidates, prefer):
    distinct = {json.dumps(item['rows'], ensure_ascii=False, sort_keys=True) for item in candidates}
    if len(distinct) == 1:
        return candidates[0]['rows']
    if not prefer:
        raise BridgeError('Conflicting histories for ' + profile +
                          '; rerun with --prefer telegram or --prefer terminal')
    preferred = [item for item in candidates if item['kind'] == prefer]
    preferred_distinct = {
        json.dumps(item['rows'], ensure_ascii=False, sort_keys=True) for item in preferred}
    if len(preferred_distinct) != 1:
        raise BridgeError('No single ' + prefer + ' history can be selected for ' + profile)
    return preferred[0]['rows']


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--telegram-state', action='append', default=[],
                        help='Legacy bridge state JSON; may be repeated')
    parser.add_argument('--terminal-history', action='append', default=[],
                        metavar='PROFILE=PATH', help='Legacy oo/oq conversation JSON')
    parser.add_argument('--prefer', choices=['telegram', 'terminal'])
    parser.add_argument('--execute', action='store_true',
                        help='Write to an empty shared history; otherwise print the plan')
    args = parser.parse_args()
    config = read_config(args.config)
    profiles = config['profiles']
    candidates = []
    telegram_paths = [Path(value).expanduser().resolve() for value in args.telegram_state]
    if not telegram_paths:
        default_state = Path(config['state_file']).expanduser().resolve()
        if default_state.exists():
            telegram_paths.append(default_state)
    for path in telegram_paths:
        candidates.extend(read_telegram_state(path, profiles))
    for value in args.terminal_history:
        candidates.append(parse_terminal(value, profiles))
    grouped = {}
    for item in candidates:
        grouped.setdefault(item['profile'], []).append(item)
    if not grouped:
        raise BridgeError('No non-empty legacy histories were found')
    selected = {profile: select_active(profile, items, args.prefer)
                for profile, items in grouped.items()}
    plan = {profile: {'candidates': len(grouped[profile]),
                      'active_messages': len(selected[profile])}
            for profile in sorted(selected)}
    if not args.execute:
        print(json.dumps({'execute': False, 'profiles': plan}, ensure_ascii=False, indent=2))
        return
    store = SessionStore(config['session_db'])
    occupied = [profile for profile in selected if store.message_count(profile)]
    if occupied:
        raise BridgeError('Shared history is not empty: ' + ', '.join(sorted(occupied)))
    for profile in sorted(selected):
        store.archive_and_replace(profile, grouped[profile], selected[profile])
    print(json.dumps({'execute': True, 'profiles': plan}, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except (BridgeError, SessionStoreError, OSError, ValueError) as error:
        print('migrate_history: ' + str(error), file=sys.stderr)
        sys.exit(1)
