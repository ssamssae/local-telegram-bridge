import io
import unittest
from unittest.mock import patch
from local_bridge import BridgeError, LocalModels, json_request

class ResponseRecoveryTests(unittest.TestCase):
    def test_non_object_json_is_a_safe_connection_error(self):
        for raw in (b'null', b'[]', b'"fixture"'):
            with self.subTest(raw=raw), patch('urllib.request.urlopen', return_value=io.BytesIO(raw)):
                with self.assertRaises(BridgeError):
                    json_request('http://localhost:1234/v1/models')

    def test_invalid_model_content_is_reported_without_crashing_worker(self):
        for provider, response in [('ollama', {'message': {'content': ['invalid']}}),
                                   ('ollama', {'message': 'invalid'}),
                                   ('lmstudio', {'choices': [{'message': {'content': ['invalid']}}]}),
                                   ('lmstudio', {'choices': ['invalid']})]:
            config = {'unload_other_profiles': False, 'max_tokens': 10,
                      'profiles': {'local': {'provider': provider, 'model': 'fixture', 'base_url': 'http://localhost:1234'}}}
            models = LocalModels(config)
            with self.subTest(provider=provider, response=response), patch('local_bridge.json_request', return_value=response), patch.object(models, 'ensure_lmstudio'):
                with self.assertRaises(BridgeError):
                    models.chat('local', [])
