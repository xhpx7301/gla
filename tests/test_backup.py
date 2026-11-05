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
        function = source[source.index("show_backup_status() {"):source.index("service_control_menu() {")]
        function = function.replace("/etc/systemd/system/", "$TEST_DIR/units/")
        with tempfile.TemporaryDirectory(prefix="gla-backup-test-") as directory:
            env = dict(os.environ, TEST_DIR=pathlib.Path(directory).as_posix())
            harness = r'''
set -Eeuo pipefail
if command -v cygpath >/dev/null 2>&1; then
  TEST_DIR="$(cygpath -u "$TEST_DIR")"
  install() { shift 3; mkdir -p "$@"; }
  # Git Bash lacks util-linux; lock contention is exercised using a failing stub below.
  flock() { return 0; }
fi
STACK_DIR="$TEST_DIR/stack"
BACKUP_STATE_DIR="$TEST_DIR/backup-state"
BACKUP_DIR="$TEST_DIR/backups"
mkdir -p "$STACK_DIR/secrets" "$TEST_DIR/volume" "$TEST_DIR/units"
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
            history = pathlib.Path(directory) / "history"
            details = history.read_text(encoding="utf-8", errors="replace") if history.exists() else ""
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr + details)

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
grep -q '^success' "$BACKUP_STATE_DIR/result"
[ -z "$(find "$TEST_DIR" -name '*.partial.*' -print -quit)" ]
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
grep -q '^failed' "$BACKUP_STATE_DIR/result"
''')

    def test_lock_contention_does_not_stop_services_or_replace_status(self):
        self.run_backup(r'''
mkdir -p "$BACKUP_STATE_DIR"
printf 'running\t123\tprevious\n' > "$BACKUP_STATE_DIR/result"
flock() { return 1; }
set +e
backup_data export "$TEST_DIR/archive.tar.gz"
result=$?
set -e
[ "$result" -ne 0 ]
[ ! -e "$TEST_DIR/compose-calls" ]
grep -q 'previous' "$BACKUP_STATE_DIR/result"
''')

    def test_auto_backup_retains_seven_and_leaves_manual_files(self):
        self.run_backup(r'''
mkdir -p "$BACKUP_DIR"
for day in 01 02 03 04 05 06 07 08; do
  printf old > "$BACKUP_DIR/gla-auto-202001$day-010000.tar.gz"
done
printf manual > "$BACKUP_DIR/gla-backup-manual.tar.gz"
run_scheduled_backup
files=("$BACKUP_DIR"/gla-auto-*.tar.gz)
[ "${#files[@]}" -eq 7 ]
[ ! -e "$BACKUP_DIR/gla-auto-20200101-010000.tar.gz" ]
[ ! -e "$BACKUP_DIR/gla-auto-20200102-010000.tar.gz" ]
[ -e "$BACKUP_DIR/gla-backup-manual.tar.gz" ]
grep -q '^success' "$BACKUP_STATE_DIR/result"
''')

    def test_failed_auto_backup_keeps_existing_backups(self):
        self.run_backup(r'''
mkdir -p "$BACKUP_DIR"
printf old > "$BACKUP_DIR/gla-auto-20200101-010000.tar.gz"
cp() { return 1; }
set +e
(set -e; run_scheduled_backup)
result=$?
set -e
[ "$result" -ne 0 ]
[ "$(cat "$BACKUP_DIR/gla-auto-20200101-010000.tar.gz")" = old ]
grep -q '^failed' "$BACKUP_STATE_DIR/result"
''')

    def test_service_resume_failure_never_publishes_backup(self):
        self.run_backup(r'''
compose() {
  printf '%s\n' "$*" >> "$TEST_DIR/compose-calls"
  case "$1" in ps) printf 'grafana\n' ;; start) return 1 ;; esac
}
set +e
backup_data export "$TEST_DIR/archive.tar.gz"
result=$?
set -e
[ "$result" -ne 0 ]
[ ! -e "$TEST_DIR/archive.tar.gz" ]
grep -q '^failed' "$BACKUP_STATE_DIR/result"
''')

    def test_timer_default_and_custom_time_use_explicit_timezone(self):
        self.run_backup(r'''
systemctl() { printf '%s\n' "$*" >> "$TEST_DIR/systemctl-calls"; }
configure_backup_timer <<< ''
grep -Fq 'OnCalendar=*-*-* 01:00:00 Asia/Shanghai' "$TEST_DIR/units/gla-backup.timer"
grep -q 'Persistent=true' "$TEST_DIR/units/gla-backup.timer"
grep -q 'backup-run' "$TEST_DIR/units/gla-backup.service"
grep -q 'restart gla-backup.timer' "$TEST_DIR/systemctl-calls"
configure_backup_timer <<< '02:35'
grep -Fq 'OnCalendar=*-*-* 02:35:00 Asia/Shanghai' "$TEST_DIR/units/gla-backup.timer"
[ "$(cat "$BACKUP_STATE_DIR/time")" = 02:35 ]
''')

    def test_invalid_time_does_not_write_units(self):
        self.run_backup(r'''
systemctl() { return 0; }
set +e
configure_backup_timer <<< '25:00'
result=$?
set -e
[ "$result" -ne 0 ]
[ ! -e "$TEST_DIR/units/gla-backup.timer" ]
''')

    def test_status_reports_timer_next_run_and_interrupted_backup(self):
        self.run_backup(r'''
date() {
  case "$*" in
    '+%s') printf '200\n' ;;
    '-d today 01:00 +%s') printf '100\n' ;;
    '-d tomorrow 01:00 +%s') printf '300\n' ;;
    '-d @300 +%Y-%m-%d %H:%M:%S UTC+8') printf '2026-11-06 01:00:00 UTC+8\n' ;;
    '-d @1793808000 +%Y-%m-%d %H:%M:%S UTC+8') printf '2026-11-05 17:20:00 UTC+8\n' ;;
    *) command date "$@" ;;
  esac
}
systemctl() { return 0; }
mkdir -p "$BACKUP_STATE_DIR"
printf '01:00\n' > "$BACKUP_STATE_DIR/time"
printf 'success\t1793808000\tarchive\n' > "$BACKUP_STATE_DIR/result"
show_backup_status > "$TEST_DIR/status"
grep -q '已启用：每天 01:00 UTC+8' "$TEST_DIR/status"
grep -q '2026-11-06 01:00:00 UTC+8' "$TEST_DIR/status"
! grep -q '15:00:00 UTC+8' "$TEST_DIR/status"
grep -q '成功' "$TEST_DIR/status"
printf 'running\t1793808000\tarchive\n' > "$BACKUP_STATE_DIR/result"
flock() { return 0; }
show_backup_status > "$TEST_DIR/status"
grep -q '异常中断' "$TEST_DIR/status"
flock() { return 1; }
show_backup_status > "$TEST_DIR/status"
grep -q '进行中' "$TEST_DIR/status"
''')

    def test_backup_history_lists_manual_export_and_explains_empty_journal(self):
        self.run_backup(r'''
mkdir -p "$BACKUP_DIR"
printf archive > "$BACKUP_DIR/gla-auto-20261105-010000.tar.gz"
printf manual > "$TEST_DIR/manual-backup.tar.gz"
mkdir -p "$BACKUP_STATE_DIR"
printf 'success\t1793808000\t%s\n' "$TEST_DIR/manual-backup.tar.gz" > "$BACKUP_STATE_DIR/result"
journalctl() { printf '%s\n' '-- No entries --'; }
show_backup_history > "$TEST_DIR/history"
grep -q 'gla-auto-20261105-010000.tar.gz' "$TEST_DIR/history"
grep -q 'manual-backup.tar.gz' "$TEST_DIR/history"
grep -q '手动导出的备份不会写入 systemd 日志' "$TEST_DIR/history"
''')

    def test_backup_history_reports_no_archives_explicitly(self):
        self.run_backup(r'''
journalctl() { printf '%s\n' '-- No entries --'; }
show_backup_history > "$TEST_DIR/history"
grep -q '尚未找到备份文件' "$TEST_DIR/history"
grep -q '暂无自动备份任务日志' "$TEST_DIR/history"
''')

    def test_generated_manager_shell_syntax(self):
        source = (ROOT / "deploy-xray-grafana-loki-alloy.sh").read_text(encoding="utf-8")
        manager = source.split('cat >"$MANAGER_PATH" <<\'EOF\'\n', 1)[1].split('\nEOF\n', 1)[0]
        result = subprocess.run([BASH, "-n"], input=manager, text=True, encoding="utf-8",
                                capture_output=True, timeout=10)
        self.assertEqual(result.returncode, 0, result.stderr)


if __name__ == "__main__":
    unittest.main()
