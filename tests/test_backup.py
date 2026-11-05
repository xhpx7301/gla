"""Exercise backup/restore against temporary files with Docker and Compose stubbed."""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).parents[1]
BASH = shutil.which("bash")
if os.name == "nt" and pathlib.Path("D:/Git/bin/bash.exe").exists():
    BASH = "D:/Git/bin/bash.exe"


@unittest.skipUnless(BASH, "Bash is required")
class BackupTest(unittest.TestCase):
    def run_backup(self, body):
        source = (ROOT / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        function = source[source.index("backup_data() ("):source.index("service_control_menu() {")]
        with tempfile.TemporaryDirectory(prefix="gla-backup-test-") as directory:
            env = dict(os.environ, TEST_DIR=pathlib.Path(directory).as_posix())
            harness = r'''
set -Eeuo pipefail
if command -v cygpath >/dev/null 2>&1; then TEST_DIR="$(cygpath -u "$TEST_DIR")"; fi
STACK_DIR="$TEST_DIR/stack"
mkdir -p "$STACK_DIR/secrets" "$TEST_DIR/volume"
printf 'config' > "$STACK_DIR/compose.yaml"
printf 'secret' > "$STACK_DIR/secrets/token"
printf 'history' > "$TEST_DIR/volume/.data"
compose() {
  printf '%s\n' "$*" >> "$TEST_DIR/compose-calls"
  if [ "$1" = ps ]; then printf 'grafana\n'; fi
}
docker() {
  if [ "$2" = ls ]; then printf 'xray-log-dashboard_grafana-data\n';
  elif [ "$2" = inspect ]; then printf '%s\n' "$TEST_DIR/volume";
  else return 1; fi
}
confirm() { return 0; }
''' + function + body
            result = subprocess.run([BASH], input=harness, text=True, encoding="utf-8",
                                    capture_output=True, env=env, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_round_trip_restores_secrets_and_hidden_volume_files(self):
        self.run_backup(r'''
backup_data <<< $'1\n'"$TEST_DIR/archive.tar.gz"
printf 'changed' > "$STACK_DIR/secrets/token"
printf 'changed' > "$TEST_DIR/volume/.data"
printf 'extra' > "$TEST_DIR/volume/extra"
backup_data <<< $'2\n'"$TEST_DIR/archive.tar.gz"
[ "$(cat "$STACK_DIR/secrets/token")" = secret ]
[ "$(cat "$TEST_DIR/volume/.data")" = history ]
[ ! -e "$TEST_DIR/volume/extra" ]
grep -q 'start grafana' "$TEST_DIR/compose-calls"
grep -q 'up -d --force-recreate' "$TEST_DIR/compose-calls"
''')

    def test_corrupt_archive_never_stops_services(self):
        self.run_backup(r'''
printf 'broken archive' > "$TEST_DIR/archive.tar.gz"
set +e
backup_data <<< $'2\n'"$TEST_DIR/archive.tar.gz"
result=$?
set -e
[ "$result" -ne 0 ]
[ ! -e "$TEST_DIR/compose-calls" ]
[ "$(cat "$TEST_DIR/volume/.data")" = history ]
''')

    def test_export_copy_failure_resumes_running_services(self):
        self.run_backup(r'''
cp() { return 1; }
set +e
backup_data <<< $'1\n'"$TEST_DIR/archive.tar.gz"
result=$?
set -e
[ "$result" -ne 0 ]
grep -q 'start grafana' "$TEST_DIR/compose-calls"
[ ! -e "$TEST_DIR/archive.tar.gz" ]
''')


if __name__ == "__main__":
    unittest.main()
