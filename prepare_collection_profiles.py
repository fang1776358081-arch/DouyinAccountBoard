"""Build a private F2 profile index from explicitly verified account reports."""
import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import tempfile

from collect_counts_batch import load_accounts, load_json
from collection_batch_state import BatchState
from collection_profiles import ProfileIndex, canonical_profile


def build_index(accounts, base_rows, verified_report, checkpoint=None):
    allowed = set(accounts)
    mapped = defaultdict(set)

    def add(row):
        if not isinstance(row, dict):
            raise ValueError("主页报告有无效记录")
        handle, url = row.get("account"), row.get("url")
        if not isinstance(handle, str):
            raise ValueError("主页报告缺少完整抖音号")
        if handle in allowed:
            if not canonical_profile(url):
                raise ValueError("已核验主页地址格式不正确")
            mapped[handle].add(url)

    for row in base_rows:
        add(row)
    if checkpoint is not None:
        for account in accounts:
            if url := checkpoint.profile(account):
                add({"account": account, "url": url})
    base_matches = sum(len(urls) == 1 for urls in mapped.values())
    for row in verified_report:
        if not isinstance(row, dict):
            raise ValueError("主页报告有无效记录")
        if row.get("status") == "已核实":
            add(row)

    url_frequency = Counter(url for urls in mapped.values() for url in urls)
    selected = []
    conflicts = 0
    for account in accounts:
        urls = mapped.get(account)
        if not urls:
            continue
        if len(urls) != 1 or any(url_frequency[url] != 1 for url in urls):
            conflicts += 1
            continue
        selected.append({"account": account, "url": next(iter(urls))})
    summary = {"targets": len(accounts), "previously_mapped": base_matches,
               "ready": len(selected), "missing": len(accounts)-len(selected)-conflicts,
               "conflicts": conflicts}
    return {"version": 1, "profiles": selected}, summary


def verified_rows(path):
    report = load_json(path)
    rows = report.get("results") if isinstance(report, dict) else None
    if not isinstance(rows, list) or len(rows) > 20000:
        raise ValueError("主页核验报告没有有效的 results")
    if report.get("source") == "抖音网页版公开主页":
        # A completed browser report only includes successful rows. Its counts
        # are evidence of the prior check and are not copied into the index.
        if (report.get("success") != len(rows) or report.get("failed") != 0
                or report.get("skipped") != 0 or not report.get("completed_at")
                or any(not isinstance(row, dict) or not canonical_profile(row.get("url"))
                       or not re.fullmatch(r"[0-9]+", str(row.get("posts"))) for row in rows)):
            raise ValueError("浏览器主页取数报告不完整")
        return [{"account": row.get("account"), "url": row["url"], "status": "已核实"} for row in rows]
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--accounts", type=Path, required=True)
    parser.add_argument("--verified-report", type=Path, action="append", required=True)
    parser.add_argument("--base-index", type=Path, required=True)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    output = args.output.resolve()
    if output in {path.resolve() for path in (args.accounts, *args.verified_report,
                                               args.base_index, args.checkpoint) if path}:
        parser.error("请用单独的输出文件保留原始资料")
    accounts = load_accounts(args.accounts)
    ProfileIndex.from_file(str(args.base_index.resolve()))
    base = load_json(args.base_index)
    try:
        rows = [row for report in args.verified_report for row in verified_rows(report)]
    except ValueError as error:
        parser.error(str(error))
    if args.checkpoint and not args.checkpoint.is_file():
        parser.error("续采记录不存在")
    state = BatchState.open(args.checkpoint, accounts) if args.checkpoint else None
    payload, summary = build_index(accounts, base["profiles"], rows, state)
    if summary["conflicts"]:
        parser.error(f'有 {summary["conflicts"]} 个账号的主页归属冲突，未生成新索引')
    output.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=output.parent,
                                     suffix=".tmp", delete=False) as temp:
        staging = Path(temp.name)
        json.dump(payload, temp, ensure_ascii=False, indent=2)
        temp.write("\n")
    try:
        ProfileIndex.from_file(str(staging))
        staging.replace(output)
    finally:
        staging.unlink(missing_ok=True)
    print(json.dumps(summary, ensure_ascii=False))


if __name__ == "__main__":
    main()
