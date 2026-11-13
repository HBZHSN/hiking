"""Render a system or user unit without executing service-management commands."""
from pathlib import Path
import re
import sys


def render(project_dir, user=''):
    if not Path(project_dir).is_absolute() or project_dir != project_dir.strip():
        raise ValueError('Project directory must be an absolute path without surrounding whitespace')
    if any(c in project_dir for c in '\n\r%"\\$'):
        raise ValueError('Unsupported systemd path characters')
    if user and not re.fullmatch(r'[a-zA-Z_][a-zA-Z0-9_.-]*', user):
        raise ValueError('Unsupported service user')
    content = (Path(__file__).parent / 'hiking.service').read_text()
    content = content.replace('@PROJECT_DIR@', project_dir)
    if user:
        content = content.replace('[Service]\n', f'[Service]\nUser={user}\n')
        content = content.replace('WantedBy=default.target', 'WantedBy=multi-user.target')
    return content


if __name__ == '__main__':
    print(render(*sys.argv[1:]), end='')
