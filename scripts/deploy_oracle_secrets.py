"""Run via sudo on the server; read selected provider env lines from stdin.

Creates staging credentials once, outside the mounted application directory.
Never prints credentials. Do not use for public multi-user authentication.
"""
import os
import pwd
from pathlib import Path
import secrets
import sys

os.umask(0o077)
target = Path('/srv/dexter.env')
if target.exists():
    raise SystemExit('Secrets already exist; refusing to overwrite them.')
providers = sys.stdin.read()
password = secrets.token_urlsafe(24)
target.write_text(providers.rstrip() + '\n'
                  + 'CHAINLIT_AUTH_SECRET=' + secrets.token_urlsafe(48) + '\n'
                  + 'DEXTER_STAGING_PASSWORD=' + password + '\n'
                  + 'LANGSMITH_TRACING=false\n', encoding='utf-8')
login = Path('/home/ubuntu/dexter-staging-login.txt')
login.write_text('Username: dexter\nPassword: ' + password + '\n', encoding='utf-8')
owner = pwd.getpwnam('ubuntu')
os.chown(login, owner.pw_uid, owner.pw_gid)
print('Staging secrets created; credentials saved in the Ubuntu home directory.')
