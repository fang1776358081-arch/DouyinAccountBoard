"""Check the running CloudBase deployment and keep a verified backup on its server."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import pwd
import re
import subprocess
import sys
import tempfile


ROOT = Path("/opt/douyin-board")
SERVICE = "douyin-board.service"


class CutoverStop(Exception):
    pass


def require(condition, reason):
    if not condition:
        raise CutoverStop(reason)


def capture(*command):
    return subprocess.check_output(command, stderr=subprocess.PIPE, timeout=20)


def service_pid():
    pid = capture("systemctl", "show", SERVICE, "-p", "MainPID", "--value").decode().strip()
    require(pid.isascii() and pid.isdigit() and int(pid) > 0, "WEBSITE_NOT_RUNNING")
    return pid


def live_environment(pid):
    proc = Path("/proc") / pid
    argv = (proc / "cmdline").read_bytes().decode().rstrip("\0").split("\0")
    require(any(Path(arg).name == "web_server.py" for arg in argv), "EXPECTED_PYTHON_SERVICE")
    require(argv.count("--config") == 1 and argv.index("--config") + 1 < len(argv), "SERVICE_CONFIG_UNCONFIRMED")
    config_path = Path(argv[argv.index("--config") + 1])
    if not config_path.is_absolute():
        config_path = (proc / "cwd").resolve() / config_path
    config = json.loads(config_path.read_text(encoding="utf-8"))
    require(config.get("enabled") is True and config.get("backend") == "cloudbase"
            and config.get("host") == "0.0.0.0", "EXPECTED_CLOUDBASE_SERVICE")
    environment = dict(
        item.split("=", 1) for item in (proc / "environ").read_bytes().decode().split("\0") if "=" in item
    )
    require(environment.get("PORT") == "80", "PUBLIC_PORT_UNCONFIRMED")
    return environment


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--expect-commit", required=True)
    parser.add_argument("--backup", action="store_true")
    args = parser.parse_args()
    require(sys.platform == "linux" and os.geteuid() == 0, "RUN_ON_EXISTING_SERVER_AS_ROOT")
    require(re.fullmatch(r"[0-9a-f]{40}", args.expect_commit), "EXACT_COMMIT_REQUIRED")
    os.umask(0o077)
    sys.dont_write_bytecode = True
    owner = pwd.getpwuid(ROOT.stat().st_uid).pw_name
    git = ("runuser", "-u", owner, "--", "git", "-C", str(ROOT))
    require(capture(*git, "rev-parse", "HEAD").decode().strip() == args.expect_commit, "UNEXPECTED_DEPLOYED_COMMIT")
    require(not capture(*git, "status", "--porcelain"), "DEPLOYED_WORKTREE_DIRTY")

    # The helpers run with the same trusted checkout used by the existing service.
    sys.path.insert(0, str(ROOT))
    from cloudbase_runtime import connect_database, settings
    from pg_schema import runtime_versions_supported, validate_extensions
    from pg_service_adapter import ServiceConnection
    from tools.upgrade_publish_review import backup, control, relations, save, versions

    pid = service_pid()
    environment = live_environment(pid)
    config = settings(environment)
    report = dict(status="checked", backend="cloudbase", sslmode=config["sslmode"],
                  schema="board_candidate", commit=args.expect_commit)
    with connect_database(environment) as db:
        db.execute("SET search_path=board_candidate,pg_catalog")
        db.execute("SET default_transaction_read_only=on")
        installed = versions(db)
        require(runtime_versions_supported(installed), "MIGRATION_LEDGER_MISMATCH")
        validate_extensions(ServiceConnection(db, schema="board_candidate", scope_enabled=True), installed)
        state = control(db)
        require(state and state[0] == "open" and state[1] >= 1 and state[2], "RUNTIME_NOT_OPEN")
        require(db.execute("SELECT 1 FROM auth_users WHERE role='owner' AND active=1").fetchone(), "NO_ACTIVE_OWNER")
        collection = db.execute("SELECT auto_enabled FROM collection_settings WHERE id=1").fetchone()
        require(collection and not collection[0], "COLLECTION_NOT_DISABLED")
        require(db.execute("SELECT ssl FROM pg_stat_ssl WHERE pid=pg_backend_pid()").fetchone() == (True,),
                "DATABASE_TLS_NOT_CONFIRMED")
        report.update(schema_version=len(installed), mode="open", postgres_major=db.info.server_version // 10000)
        if args.backup:
            folder = Path(tempfile.mkdtemp(prefix="douyin-go-cutover-", dir="/root"))
            print(json.dumps(dict(phase="backup-started", folder=str(folder))), flush=True)
            result = backup(db, config, folder, "/usr/bin/pg_dump", "/usr/bin/pg_restore",
                            dict(relations=relations(db)))
            target = folder / "before-go-cutover.dump"
            Path(result["path"]).rename(target)
            result["path"] = str(target)
            with target.open("rb") as archived:
                require(hashlib.file_digest(archived, "sha256").hexdigest() == result["sha256"], "BACKUP_HASH_MISMATCH")
            require(service_pid() == pid and versions(db) == installed and control(db) == state,
                    "PRODUCTION_STATE_CHANGED_DURING_BACKUP")
            report.update(status="backup-verified", backup=result)
            save(folder, report)
    print(json.dumps(report), flush=True)


if __name__ == "__main__":
    try:
        main()
    except CutoverStop as error:
        print(f"STOP: {error}", file=sys.stderr)
        sys.exit(1)
    except Exception as error:
        # PostgreSQL and system command diagnostics can include deployment secrets.
        print(f"STOP: {type(error).__name__}", file=sys.stderr)
        sys.exit(1)
