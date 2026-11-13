import safety  # Install protection before importing application code.
import getpass
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from deploy.render_service import render


class ServiceTests(unittest.TestCase):
    def test_system_service_runs_as_original_user(self):
        unit = render('/srv/my hiking', 'hiking-user')
        self.assertIn('User=hiking-user\n', unit)
        self.assertIn('WantedBy=multi-user.target', unit)
        self.assertIn('ExecStart="/srv/my hiking/.venv/bin/python" -m backend.server', unit)
        self.assertIn('WorkingDirectory=/srv/my hiking', unit)
        self.assertNotIn('@PROJECT_DIR@', unit)

    def test_user_service_and_reject_injected_directives(self):
        unit = render('/srv/hiking')
        self.assertNotIn('User=', unit)
        self.assertIn('WantedBy=default.target', unit)
        for path in ['/srv/\nhiking', '/srv/%h', '/srv/$NAME', '/srv/"hiking']:
            with self.assertRaises(ValueError):
                render(path)
        with self.assertRaises(ValueError):
            render('/srv/hiking', 'hiking-user\nUser=root')


    @unittest.skipUnless(shutil.which('systemd-analyze'), 'systemd-analyze unavailable')
    def test_generated_units_pass_real_systemd_validation(self):
        root = Path(__file__).resolve().parent.parent
        with tempfile.TemporaryDirectory() as directory:
            # Exercise spaces in both paths using the actual project interpreter.
            linked = Path(directory) / 'project with spaces'
            linked.symlink_to(root, target_is_directory=True)
            unit = Path(directory) / 'hiking.service'
            for user in ('', getpass.getuser()):
                with self.subTest(user=user):
                    unit.write_text(render(str(linked), user))
                    result = subprocess.run(
                        ['systemd-analyze', 'verify', str(unit)],
                        env={**os.environ, 'SYSTEMD_UNIT_PATH': directory + ':/usr/lib/systemd/system:/lib/systemd/system'},
                        capture_output=True, text=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
