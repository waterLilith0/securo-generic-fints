#!/usr/bin/env python3
"""Set your real GitHub repository before publishing a plugin."""
import json
import re
import sys
from pathlib import Path

if len(sys.argv) not in (2, 3):
    raise SystemExit('Usage: python3 configure-repository.py GITHUB_OWNER [REPO_NAME]')
owner = sys.argv[1].lower()
repo = (sys.argv[2] if len(sys.argv) == 3 else 'generic-fints').lower()
if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,38}', owner) or not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', repo):
    raise SystemExit('Use lower-case GitHub owner/repo names with letters, digits and hyphens')
path = Path(__file__).resolve().parent / 'securo-plugin.json'
man = json.loads(path.read_text(encoding='utf-8'))
man['id'] = f'{owner}/{repo}'
man['repository'] = f'https://github.com/{owner}/{repo}'
path.write_text(json.dumps(man, indent=2, ensure_ascii=False) + '\n', encoding='utf-8')
print('Manifest updated:', man['id'])
