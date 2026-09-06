#!/usr/bin/env python3
"""Install a per-user macOS LaunchAgent using an existing private configuration."""
import argparse
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

from local_bridge import read_config


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', required=True)
    parser.add_argument('--instance', default='', help='Separate service and logs, e.g. qwen')
    args = parser.parse_args()
    if args.instance and not re.fullmatch('[a-z0-9][a-z0-9_-]{0,31}', args.instance):
        parser.error('instance must use 1–32 lowercase letters, digits, underscores or hyphens')
    if sys.platform != 'darwin':
        raise SystemExit('This installer is for macOS; run local_bridge.py directly on Linux.')
    config = Path(args.config).expanduser().resolve()
    read_config(config)
    config.chmod(0o600)
    home = Path.home()
    dest = home / '.local/share/local-telegram-bridge'
    suffix = '-' + args.instance if args.instance else ''
    logs = home / ('Library/Logs/local-telegram-bridge' + suffix)
    dest.mkdir(parents=True, exist_ok=True)
    logs.mkdir(parents=True, exist_ok=True)
    source = Path(__file__).resolve().with_name('local_bridge.py')
    target = dest / 'local_bridge.py'
    if source != target:
        shutil.copy2(source, target)
    label = 'com.local-telegram-bridge' + suffix
    plist = home / 'Library/LaunchAgents' / (label + '.plist')
    plist.parent.mkdir(parents=True, exist_ok=True)
    value = {'Label': label,
             'ProgramArguments': [sys.executable, str(target), '--config', str(config)],
             'WorkingDirectory': str(home), 'RunAtLoad': True, 'KeepAlive': True,
             'ThrottleInterval': 10,
             'EnvironmentVariables': {'PYTHONUNBUFFERED': '1',
                 'PATH': str(home / '.lmstudio/bin') + ':/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin'},
             'StandardOutPath': str(logs / 'bridge.log'),
             'StandardErrorPath': str(logs / 'bridge.err')}
    plist.write_bytes(plistlib.dumps(value))
    plist.chmod(0o600)
    domain = 'gui/' + str(os.getuid())
    subprocess.run(['/bin/launchctl', 'bootout', domain + '/' + label], capture_output=True)
    subprocess.run(['/bin/launchctl', 'bootstrap', domain, str(plist)], check=True)
    print('Installed ' + str(plist))
    print('Logs: ' + str(logs))


if __name__ == '__main__':
    main()
