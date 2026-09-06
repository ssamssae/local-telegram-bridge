import importlib.util
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('install_macos', Path(__file__).parents[1] / 'install_macos.py')
installer = importlib.util.module_from_spec(spec)
spec.loader.exec_module(installer)


class InstallTests(unittest.TestCase):
    def test_named_instance_preserves_original_service_and_separates_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            config = home / 'qwen.json'
            config.write_text('{}')
            agents = home / 'Library/LaunchAgents'
            agents.mkdir(parents=True)
            original = agents / 'com.local-telegram-bridge.plist'
            original.write_bytes(b'existing service')
            with patch.object(installer.Path, 'home', return_value=home), \
                 patch.object(installer, 'read_config'), \
                 patch.object(installer.sys, 'platform', 'darwin'), \
                 patch.object(installer.sys, 'argv', ['install_macos.py', '--config', str(config), '--instance', 'qwen']), \
                 patch.object(installer.subprocess, 'run') as run:
                installer.main()
            self.assertEqual(b'existing service', original.read_bytes())
            plist = plistlib.loads((agents / 'com.local-telegram-bridge-qwen.plist').read_bytes())
            self.assertEqual('com.local-telegram-bridge-qwen', plist['Label'])
            self.assertIn('local-telegram-bridge-qwen/', plist['StandardOutPath'])
            self.assertEqual(str(config.resolve()), plist['ProgramArguments'][-1])
            self.assertTrue(run.call_args_list[0].args[0][-1].endswith('/com.local-telegram-bridge-qwen'))

    def test_invalid_instance_cannot_escape_service_directory(self):
        with patch.object(installer.sys, 'argv', ['install_macos.py', '--config', '/missing', '--instance', '../other']):
            with self.assertRaises(SystemExit) as error:
                installer.main()
        self.assertEqual(2, error.exception.code)


if __name__ == '__main__':
    unittest.main()
