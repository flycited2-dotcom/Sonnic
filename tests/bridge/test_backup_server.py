"""Exercise the maintenance shell with fake commands; never reaches Docker or a DB."""
import os
from pathlib import Path
import subprocess
import tempfile
import unittest


@unittest.skipIf(os.name == 'nt', 'POSIX maintenance shell runs on the VPS')
class BackupMaintenanceTests(unittest.TestCase):
    def check_scenario(self, scenario):
        with tempfile.TemporaryDirectory(prefix='sonnic-maintenance-test-') as temp:
            root = Path(temp)
            (root / 'source/deploy').mkdir(parents=True)
            commands = root / 'bin'
            commands.mkdir()
            fake = '''#!/bin/sh
name=${0##*/}
printf '%s %s\n' "$name" "$*" >> "$TEST_LOG"
case "$name" in
  docker)
    case "$1" in
      inspect) echo true ;;
      exec) cat >/dev/null; test "$TEST_SCENARIO" != active ;;
    esac ;;
  df) printf 'Filesystem blocks Used Available Capacity Mounted\nmock 9000000 1 8000000 1%% /\n' ;;
  python3) test "$TEST_SCENARIO" != failure ;;
esac
'''
            for name in ('docker', 'df', 'python3', 'flock'):
                path = commands / name
                path.write_text(fake)
                path.chmod(0o700)
            original = Path(os.environ.get('SONNIC_BACKUP_SCRIPT',
                            Path(__file__).resolve().parents[2] / 'scripts/backup-server.sh'))
            script = root / 'backup.sh'
            script.write_text(original.read_text().replace('/opt/sonnic', str(root)))
            log = root / 'commands.log'
            env = os.environ | {'PATH': str(commands) + ':/usr/bin:/bin',
                                'TEST_LOG': str(log), 'TEST_SCENARIO': scenario}
            result = subprocess.run(['/bin/sh', str(script), 'scheduled-20260909T000000Z'],
                                    env=env, capture_output=True, timeout=10)
            return result.returncode, log.read_text()

    def test_success_restarts_same_services(self):
        code, log = self.check_scenario('success')
        self.assertEqual(code, 0)
        self.assertIn('docker compose start api web gateway', log)
        self.assertNotIn('compose up', log)

    def test_backup_failure_still_restarts_services(self):
        code, log = self.check_scenario('failure')
        self.assertNotEqual(code, 0)
        self.assertIn('docker compose stop -t 20 web api', log)
        self.assertIn('docker compose start api web gateway', log)

    def test_active_job_skips_database_shutdown_and_backup(self):
        code, log = self.check_scenario('active')
        self.assertNotEqual(code, 0)
        self.assertNotIn('docker compose stop -t 20 web api', log)
        self.assertNotIn('python3 ', log)
        self.assertIn('docker compose start api web gateway', log)


if __name__ == '__main__':
    unittest.main()
