"""Check staging authentication without initializing models or live storage."""
import ast
import asyncio
import hmac
from pathlib import Path
from types import SimpleNamespace
import unittest


def auth_callbacks(password):
    callbacks = {}

    def register(kind):
        def decorator(fn):
            callbacks[kind] = fn
            return fn
        return decorator

    source = ast.parse(Path('chainlit_app.py').read_text(encoding='utf-8'))
    block = next(node for node in source.body if isinstance(node, ast.If)
                 and 'DEXTER_STAGING_PASSWORD' in ast.unparse(node.test))
    module = ast.Module(body=[block], type_ignores=[])
    exec(compile(module, 'chainlit_app.py', 'exec'), {
        'os': SimpleNamespace(environ={'DEXTER_STAGING_PASSWORD': password} if password else {}),
        'hmac': hmac,
        '_FIXED_USER_IDENTIFIER': 'user',
        'cl': SimpleNamespace(password_auth_callback=register('password'),
                              header_auth_callback=register('header'), User=SimpleNamespace),
    })
    return callbacks


class StagingAuthTests(unittest.IsolatedAsyncioTestCase):
    async def test_staging_has_no_header_auth_bypass(self):
        callbacks = auth_callbacks('test-secret')
        self.assertEqual(set(callbacks), {'password'})
        login = callbacks['password']
        self.assertIsNone(await login('dexter', 'incorrect'))
        self.assertIsNone(await login('visitor', 'test-secret'))
        self.assertIsNone(await login('訪客', 'test-secret'))
        self.assertIsNone(await login('dexter', '密碼'))
        self.assertEqual((await login('dexter', 'test-secret')).identifier, 'user')

    async def test_local_behavior_is_preserved(self):
        callbacks = auth_callbacks(None)
        self.assertEqual(set(callbacks), {'header'})
        self.assertEqual((await callbacks['header']({})).identifier, 'user')


if __name__ == '__main__':
    unittest.main()
