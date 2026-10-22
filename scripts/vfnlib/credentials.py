"""Load local Feishu credentials without evaluating shell syntax."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import dotenv_values

from .config import ROOT

CREDENTIAL_KEYS = (
    'FEISHU_APP_ID', 'FEISHU_APP_SECRET',
    'FEISHU_USER_ACCESS_TOKEN', 'FEISHU_TENANT_ACCESS_TOKEN',
)


def load_credentials(env_file=None):
    """Existing environment wins; only the default file is optional."""
    chosen = env_file or os.environ.get('VFN_ENV_FILE')
    path = Path(chosen).expanduser().resolve() if chosen else ROOT / '.env.local'
    if not path.exists() and not chosen:
        return None
    if not path.is_file():
        raise ValueError('Credential file not found: ' + str(path))
    values = dotenv_values(path, encoding='utf-8-sig', interpolate=False)
    for key in CREDENTIAL_KEYS:
        value = values.get(key)
        if value is not None:
            os.environ.setdefault(key, value)
    return path
