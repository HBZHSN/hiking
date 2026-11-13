import os
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import patch

import safety


class SafetyTests(unittest.TestCase):
    def test_production_files_and_database_are_blocked(self):
        for path in (safety.ROOT / '.env', safety.ROOT / 'data/hiking.sqlite3',
                     safety.ROOT / 'data/photos/1/test.jpg',
                     safety.ROOT / 'data/remote-cache/test'):
            for mode in ('r', 'w', 'a'):
                with self.subTest(path=path.name, mode=mode), self.assertRaises(RuntimeError):
                    # Emit the real audit event without opening or modifying any file.
                    sys.audit('open', str(path), mode, 0)
        database = safety.ROOT / 'data/hiking.sqlite3'
        for path in (str(database), os.fsencode(database), database.as_uri() + '?mode=rw'):
            with self.subTest(path=path), self.assertRaises(RuntimeError):
                sys.audit('sqlite3.connect', path)

    def test_delete_rename_and_symlinks_cannot_reach_production(self):
        protected = str(safety.ROOT / 'data/photos/1/test.jpg')
        for event, args in (
            ('os.remove', (protected, -1)),
            ('os.rmdir', (str(safety.ROOT / 'data'), -1)),
            ('os.rename', ('/tmp/test-photo', protected, -1, -1)),
            ('os.rename', (protected, '/tmp/test-photo', -1, -1)),
            ('os.mkdir', (str(safety.ROOT / 'data/test'), 0o700, -1)),
            ('shutil.rmtree', (str(safety.ROOT / 'data/photos'), None)),
        ):
            with self.subTest(event=event, args=args), self.assertRaises(RuntimeError):
                sys.audit(event, *args)
        with tempfile.TemporaryDirectory() as directory:
            alias = Path(directory) / 'production'
            alias.symlink_to(safety.ROOT / 'data', target_is_directory=True)
            with self.assertRaises(RuntimeError):
                sys.audit('open', str(alias / 'hiking.sqlite3'), 'w', 0)

    def test_network_is_blocked_and_temporary_database_still_works(self):
        for event in ('socket.connect', 'socket.bind', 'socket.getaddrinfo', 'socket.sendto'):
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                sys.audit(event, None, ('127.0.0.1', 8010))
        with tempfile.TemporaryDirectory() as directory:
            with sqlite3.connect(Path(directory) / 'test.sqlite3') as database:
                database.execute('CREATE TABLE example (value TEXT)')
                database.execute("INSERT INTO example VALUES ('isolated')")
                self.assertEqual(database.execute('SELECT value FROM example').fetchone()[0], 'isolated')

    def test_album_reads_and_scans_are_blocked(self):
        album = str(safety.ROOT / '.local/photo-source')
        for event, args in (('os.scandir', (album,)), ('os.listdir', (album,)),
                            ('open', (album + '/example.heic', 'r', 0))):
            with self.subTest(event=event), self.assertRaises(RuntimeError):
                sys.audit(event, *args)

    def test_browser_network_allowance_is_only_one_temporary_loopback_server(self):
        with patch.object(safety, 'BROWSER_PORT', 43210):
            sys.audit('socket.bind', None, ('127.0.0.1', 0))
            sys.audit('socket.connect', None, ('127.0.0.1', 43210))
            for event, address in (('socket.bind', ('127.0.0.1', 8010)),
                                   ('socket.bind', ('0.0.0.0', 0)),
                                   ('socket.connect', ('127.0.0.1', 8010)),
                                   ('socket.connect', ('example.com', 43210))):
                with self.subTest(event=event, address=address), self.assertRaises(RuntimeError):
                    sys.audit(event, None, address)
