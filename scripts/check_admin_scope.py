"""Ensure every host configuration field has one scope-inventory entry."""

from __future__ import annotations

import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from fllbot.bots import BotDefaults  # noqa: E402
from fllbot.config import AppConfig, LLMProviderConfig  # noqa: E402
from fllbot.policy import GroupPolicy  # noqa: E402


def check() -> list[str]:
    config = AppConfig()
    defaults = {f'{section}.{field}': getattr(getattr(config, section), field) for section in AppConfig.model_fields
                for field in type(getattr(config, section)).model_fields}
    for prefix, model in [('llm.providers.*', LLMProviderConfig), ('bot_defaults', BotDefaults), ('group_policy', GroupPolicy)]:
        instance = model()
        defaults.update({f'{prefix}.{field}': getattr(instance, field) for field in model.model_fields})
    expected = set(defaults)
    document = (ROOT/'docs/admin-scope.md').read_text(encoding='utf-8')
    actual = Counter(re.findall(r'^\| `([^`]+)` \|', document, re.MULTILINE))
    errors = ([f'Missing field: {field}' for field in sorted(expected-set(actual))]
            + [f'Unknown field: {field}' for field in sorted(set(actual)-expected)]
            + [f'Duplicate field: {field}' for field, count in actual.items() if count != 1])
    for field, encoded in re.findall(r'^\| `([^`]+)` \| `([^`]+)` \|', document, re.MULTILINE):
        if field in defaults and json.loads(encoded.replace('\\|', '|')) != defaults[field]:
            errors.append(f'Default changed: {field}')
    return errors


if __name__ == '__main__':
    errors = check()
    if errors:
        print('\n'.join(errors))
        raise SystemExit(1)
    print('Admin scope field coverage OK.')
