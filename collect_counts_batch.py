"""Resume a local F2 count batch from complete Douyin handles."""
import argparse
import csv
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile

from collection_batch_state import BatchState
from collection_profiles import ProfileIndex, ProfileRequired, canonical_profile
from f2_worker import lock_worker, original_script

MAX_REPORT_BYTES = 8 * 1024 * 1024
BLOCKED = {"SearchNeedsVerification", "LoginRequired"}


def load_json(path):
    with path.open("rb") as source:
        data = source.read(MAX_REPORT_BYTES + 1)
    if len(data) > MAX_REPORT_BYTES:
        raise ValueError("采集输入或结果文件太大")
    return json.loads(data.decode("utf-8-sig"))


def load_accounts(path):
    if path.suffix.lower() == ".json":
        items = load_json(path)
    else:
        items = [line.strip() for line in path.read_text(encoding="utf-8-sig").splitlines() if line.strip()]
    if not isinstance(items, list) or not items or len(items) > 20000:
        raise ValueError("请输入完整抖音号列表")
    for item in items:
        if (not isinstance(item, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,64}", item)
                or re.fullmatch(r"MS4w[A-Za-z0-9_-]+", item)):
            raise ValueError("只接受完整抖音号字符串，保留前导零")
    return list(dict.fromkeys(items))


def phase_inputs(accounts, index, state):
    confirmed, missing, conflicts = [], [], []
    for handle in accounts:
        saved = state.profile(handle)
        try:
            url = index.resolve(handle)
        except ProfileRequired as error:
            if error.code == "ProfileConflict":
                conflicts.append({"account": handle, "status": "failed", "error": error.code})
                continue
            url = saved
        if saved and url != saved:
            conflicts.append({"account": handle, "status": "failed", "error": "ProfileConflict"})
        elif url:
            confirmed.append({"account": handle, "url": url})
        else:
            missing.append({"account": handle})
    return confirmed, missing, conflicts


def attempt_rows(reports, items):
    if len(reports) != 1:
        raise ValueError("技能没有返回唯一的采集报告")
    payload = load_json(reports[0])
    rows = payload.get("results") if isinstance(payload, dict) else None
    if not isinstance(rows, list) or len(rows) > len(items):
        raise ValueError("采集结果格式不正确")
    valid_inputs = {item["account"]: item.get("url") for item in items}
    seen = set()
    for row in rows:
        handle = row.get("account") if isinstance(row, dict) else None
        if not isinstance(handle, str) or handle not in valid_inputs or handle in seen:
            raise ValueError("采集结果账号与请求不一致")
        seen.add(handle)
        if row.get("status") == "success" and valid_inputs[handle] and row.get("url") != valid_inputs[handle]:
            raise ValueError("采集结果主页与已确认主页不一致")
    return rows


def run_skill(args, script, items, label):
    attempt_root = Path(tempfile.mkdtemp(prefix=label + "-", dir=args.output))
    source = attempt_root / "accounts.json"
    source.write_text(json.dumps(items, ensure_ascii=False), encoding="utf-8")
    command = [sys.executable, "-X", "utf8", str(script), "query",
               "--profile", str(args.profile), "--input", str(source),
               "--output", str(attempt_root), "--max-failures", "3"]
    if args.browser:
        command += ["--browser-executable", str(args.browser)]
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("DOUYIN_BOARD_", "BOARD_GO_"))
           and k not in ("DATABASE_URL", "PGPASSWORD", "PGPASSFILE")}
    try:
        code = subprocess.run(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, env=env).returncode
    except KeyboardInterrupt:
        code = 130
    reports = list(attempt_root.glob("*/results.json"))
    if not reports and code in (1, 130):
        return [], "interrupted" if code == 130 else "collector_startup_failed"
    rows = attempt_rows(reports, items)
    if code == 0 and (len(rows) != len(items) or any(row.get("status") != "success" for row in rows)):
        raise ValueError("技能报告与成功退出状态不一致")
    if code == 130:
        return rows, "interrupted"
    if code not in (0, 2):
        return rows, "collector_failed"
    reason = next((row["error"] for row in rows if row.get("status") == "failed"
                   and isinstance(row.get("error"), str) and row["error"] in BLOCKED), None)
    if not reason and any(row.get("error") == "ConsecutiveFailureLimit" for row in rows):
        reason = "consecutive_failures"
    return rows, reason


def export_csv(path, rows):
    with tempfile.NamedTemporaryFile("w", encoding="utf-8-sig", newline="",
                                     dir=path.parent, suffix=".tmp", delete=False) as stream:
        temp = Path(stream.name)
        try:
            writer = csv.writer(stream)
            writer.writerow(["完整抖音号", "主页", "作品数", "采样时间", "状态", "原因"])
            for row in rows:
                account = row["account"]
                writer.writerow(["'" + account if account.startswith("-") else account,
                                 row.get("url", ""), row.get("count"), row.get("observed_at", ""),
                                 row["status"], row.get("error", "")])
        except BaseException:
            stream.close()
            temp.unlink(missing_ok=True)
            raise
    try:
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


def parse_arguments(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, required=True, help="JSON 或逐行完整抖音号")
    parser.add_argument("--output", type=Path, required=True, help="保留进度的本机专用目录")
    parser.add_argument("--profile", type=Path, required=True, help="F2 已配置的独立浏览器目录")
    parser.add_argument("--profile-index", type=Path)
    parser.add_argument("--browser", type=Path)
    parser.add_argument("--search-skill", type=Path, help="本机已安装支持完整号搜索的 F2 技能脚本")
    parser.add_argument("--seed-report", type=Path, action="append", default=[],
                        help="导入此批次已实际验证的报告，保留原始采样时间")
    parser.add_argument("--prepare-only", action="store_true", help="保存已确认结果并输出待采清单")
    args = parser.parse_args(argv)
    if not args.prepare_only and not args.profile.is_dir():
        parser.error("独立浏览器目录不存在")
    if args.search_skill and not args.search_skill.is_file():
        parser.error("抖音号搜索技能不存在")
    return args


def read_seed_report(path):
    report = load_json(path)
    rows = report.get("results") if isinstance(report, dict) else None
    if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
        raise ValueError("已有采集报告格式不正确")
    return [row for row in rows if row.get("status") == "success"]


def recover_attempts(output, accounts, state):
    allowed = set(accounts)
    for folder in sorted(path for path in output.iterdir()
                         if path.is_dir() and path.name.startswith(("known-", "search-"))):
        reports = list(folder.glob("*/results.json"))
        if not reports:
            continue
        try:
            items = load_json(folder / "accounts.json")
            if (not isinstance(items, list) or not items
                    or any(not isinstance(item, dict)
                           or not isinstance(item.get("account"), str) or item["account"] not in allowed
                           or ("url" in item and not canonical_profile(item["url"])) for item in items)
                    or len({item["account"] for item in items}) != len(items)):
                raise ValueError("批次记录与原账号清单不一致")
            rows = attempt_rows(reports, items)
        except json.JSONDecodeError:
            # An abrupt shutdown may leave the last report only partly written.
            continue
        state.merge(row for row in rows if row.get("status") == "success")


def save_result(state, output):
    state.save(output / "checkpoint.json")
    export_csv(output / "作品采集结果.csv", state.rows())


def collect_pending(args, state, index, runner=run_skill):
    confirmed, missing, conflicts = phase_inputs(state.pending_handles(), index, state)
    state.merge(conflicts)
    save_result(state, args.output)
    if not (confirmed or missing):
        return None
    try:
        with (args.profile / ".douyin-board-worker.lock").open("a+b") as guard:
            try:
                lock_worker(guard)
            except OSError:
                return "collector_in_use"
            phases = ((original_script, confirmed, "known"),
                      (lambda: args.search_skill, missing, "search"))
            for script, items, label in phases:
                if not items:
                    continue
                chosen = script()
                if chosen is None:
                    return "search_skill_not_configured"
                rows, reason = runner(args, chosen, items, label)
                state.merge(row for row in rows if row.get("status") != "skipped")
                save_result(state, args.output)
                if reason:
                    return reason
    except FileNotFoundError:
        return "collector_profile_missing"
    return None


def main(argv=None):
    args = parse_arguments(argv)
    accounts = load_accounts(args.input)
    args.output.mkdir(parents=True, exist_ok=True)
    with (args.output / ".batch.lock").open("a+b") as guard:
        try:
            lock_worker(guard)
        except OSError:
            raise ValueError("此批次已有采集正在运行") from None
        state = BatchState.open(args.output / "checkpoint.json", accounts)
        recover_attempts(args.output, accounts, state)
        for report in args.seed_report:
            state.merge(read_seed_report(report))
        save_result(state, args.output)
        reason = "prepared_only" if args.prepare_only else None
        if not args.prepare_only:
            index = ProfileIndex.from_file(str(args.profile_index.resolve()) if args.profile_index else "")
            reason = collect_pending(args, state, index)
        pending = len(state.pending_handles())
        print(json.dumps({"target": len(accounts), "success": len(accounts)-pending,
                          "pending": pending, "stop_reason": reason}, ensure_ascii=False))
        return 2 if pending and not args.prepare_only else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (ValueError, OSError) as error:
        print(str(error), file=sys.stderr)
        sys.exit(1)
