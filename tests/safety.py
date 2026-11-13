"""Fail before unit tests access production data or make network requests."""
import os
from pathlib import Path
import sys
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
PROTECTED = tuple(path for name in ('data', '.env', '.local/photo-source')
                  for path in (ROOT / name, (ROOT / name).resolve()))
if os.environ.get('PHOTO_SOURCE'):
    PROTECTED += (Path(os.environ['PHOTO_SOURCE']).expanduser().resolve(),)
# Browser checks may opt into one isolated loopback server; unit tests keep this unset.
BROWSER_PORT = None
PATH_EVENTS = {
    'os.scandir': ((0, None),),
    'os.listdir': ((0, None),),
    'open': ((0, None),),
    'sqlite3.connect': ((0, None),),
    'os.remove': ((0, 1),),
    'os.rmdir': ((0, 1),),
    'os.mkdir': ((0, 2),),
    'os.rename': ((0, 2), (1, 3)),
    'os.link': ((0, 2), (1, 3)),
    'os.symlink': ((1, 2),),
    'os.truncate': ((0, None),),
    'os.chmod': ((0, 2),),
    'os.utime': ((0, 3),),
    'shutil.rmtree': ((0, 1),),
    'shutil.copyfile': ((0, None), (1, None)),
    'shutil.copytree': ((0, None), (1, None)),
}


def guard(event, args):
    if event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo',
                 'socket.sendto', 'socket.sendmsg'):
        if BROWSER_PORT is not None and event in ('socket.bind', 'socket.connect'):
            if args[1] == ('127.0.0.1', 0 if event == 'socket.bind' else BROWSER_PORT):
                return
        raise RuntimeError('测试禁止网络访问；请直接调用临时 WSGI 应用或模拟外部资源')
    for index, fd_index in PATH_EVENTS.get(event, ()):
        value = args[index]
        if isinstance(value, int):
            continue
        value = os.fsdecode(value)
        if event == 'sqlite3.connect' and value.startswith('file:'):
            value = unquote(urlsplit(value).path)
        path = Path(value)
        if fd_index is not None and args[fd_index] not in (None, -1) and not path.is_absolute():
            path = Path(os.readlink(f'/proc/self/fd/{args[fd_index]}')) / path
        # Removing/moving a symlink changes the link itself, not its target.
        resolved = (path.parent.resolve() / path.name if event in
                    ('os.remove', 'os.rmdir', 'os.rename', 'os.symlink') else path.resolve())
        for candidate in (path.absolute(), resolved):
            if any(candidate.is_relative_to(protected) for protected in PROTECTED):
                raise RuntimeError(f'测试禁止访问正式数据或配置：{event}')


# This affects only the test process, including imports before a test's setUp.
sys.addaudithook(guard)
