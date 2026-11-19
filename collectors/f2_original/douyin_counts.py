"""Read public Douyin post counts with F2 and a local, user-owned session."""

import argparse
import asyncio
import csv
import datetime as dt
import html
import json
import logging
import re
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

F2_COMMIT = "f6be8c0ffba9a127075bbeafe4838716650b6325"
TZ = dt.timezone(dt.timedelta(hours=8))


def now():
    return dt.datetime.now(TZ).isoformat(timespec="seconds")


def load_accounts(path):
    text = path.read_text(encoding="utf-8-sig")
    values = json.loads(text) if path.suffix.lower() == ".json" else text.splitlines()
    if not isinstance(values, list):
        raise ValueError("Input must be a JSON array or one profile URL per line")
    result, seen = [], set()
    for value in values:
        if isinstance(value, str):
            if not value.strip():
                continue
            value = {"url": value.strip()}
        if not isinstance(value, dict) or not isinstance(value.get("url"), str):
            raise ValueError("Each item needs a profile URL")
        raw = value["url"].strip()
        if re.fullmatch(r"MS4w[A-Za-z0-9_-]+", raw):
            uid = raw
        else:
            parsed = urlparse(raw)
            match = re.fullmatch(r"/user/(MS4w[A-Za-z0-9_-]+)/?", parsed.path)
            if parsed.scheme != "https" or parsed.netloc not in {"www.douyin.com", "douyin.com"} or not match:
                raise ValueError("Use a full https://www.douyin.com/user/MS4w... profile URL; a numeric handle is not sec_uid")
            uid = match[1]
        expected = value.get("account")
        if expected is not None and not isinstance(expected, str):
            raise ValueError("Optional account must be a string")
        if uid not in seen:
            seen.add(uid)
            result.append({"sec_uid": uid, "url": "https://www.douyin.com/user/" + uid, "expected_account": expected})
    if not result:
        raise ValueError("No profile URLs supplied")
    return result


class LoginRequired(Exception):
    pass


class IdentityMismatch(Exception):
    pass


class ProfileUnavailable(Exception):
    pass


def parse_user(data, item):
    user = data.get("user") or {}
    account = user.get("unique_id") or user.get("short_id")
    count = user.get("aweme_count")
    if user.get("secret") == 1 or not account or type(count) is not int or count < 0:
        raise ProfileUnavailable()
    if user.get("sec_uid") != item["sec_uid"] or (item["expected_account"] and str(account) != item["expected_account"]):
        raise IdentityMismatch()
    return {"account": str(account), "name": user.get("nickname") or "", "count": count}


def csv_safe(value):
    if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


def write_results(folder, report):
    folder.mkdir(parents=True, exist_ok=True)
    rows = report["results"]
    report["summary"] = {s: sum(r["status"] == s for r in rows) for s in ("success", "failed", "skipped")}
    target = folder / "results.json"
    temporary = folder / "results.tmp"
    temporary.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    columns = ["account", "name", "count", "url", "status", "error", "observed_at"]
    with (folder / "results.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(columns)
        writer.writerows([[csv_safe(r.get(c, "")) for c in columns] for r in rows])
    rendered = []
    for row in rows:
        name = html.escape(row.get("name") or row.get("account") or "主页")
        link = html.escape(row["url"], quote=True)
        cells = [f'<a href="{link}" target="_blank" rel="noopener noreferrer">{name}</a>']
        cells += [html.escape(str(row.get(k, "") if row.get(k) is not None else "")) for k in ("account", "count", "status", "error", "observed_at")]
        rendered.append("<tr>" + "".join("<td>" + c + "</td>" for c in cells) + "</tr>")
    body = '<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>抖音作品数量</title><style>body{font:16px sans-serif;margin:32px}table{border-collapse:collapse}th,td{padding:10px;border:1px solid #ccc}a{color:#1264b7}</style><h1>抖音作品数量</h1><p>查询时间均为北京时间。点击昵称打开主页；失败数量留空。</p><table><thead><tr><th>昵称／主页</th><th>抖音号</th><th>作品数</th><th>状态</th><th>错误</th><th>查询时间</th></tr></thead><tbody>'
    (folder / "results.html").write_text(body + "".join(rendered) + "</tbody></table></html>", encoding="utf-8")


async def run(args):
    from playwright.async_api import async_playwright

    items = load_accounts(args.input) if args.command == "query" else []
    args.profile = args.profile.expanduser().resolve()
    if args.command == "query" and not args.profile.is_dir():
        raise ValueError("Profile does not exist; run login first")
    async with async_playwright() as playwright:
        options = {"headless": args.command != "login", "locale": "zh-CN", "viewport": {"width": 1440, "height": 1000}}
        if args.browser_executable:
            options["executable_path"] = str(args.browser_executable.expanduser().resolve())
        context = await playwright.chromium.launch_persistent_context(str(args.profile), **options)
        try:
            page = await context.new_page()
            if args.command == "login":
                try:
                    await page.goto("https://www.douyin.com/", wait_until="domcontentloaded", timeout=40000)
                except Exception:
                    print("Page load timed out; finish loading in the browser.", flush=True)
                await asyncio.to_thread(input, "在独立窗口正常登录并手动完成验证，然后回到此处按回车：")
                cookies = await context.cookies(["https://www.douyin.com/"])
                ok = any(c["name"] in {"sessionid", "sessionid_ss"} and c["value"] for c in cookies)
                print("Login session saved." if ok else "No login session detected; run login again.")
                return 0 if ok else 2

            # Suppress upstream debug logs: signed URLs and session values are not report data.
            logging.disable(logging.CRITICAL)
            from f2.apps.douyin.crawler import DouyinCrawler
            from f2.apps.douyin.model import UserProfile
            from f2.apps.douyin.utils import ClientConfManager

            folder = args.output.expanduser().resolve() / (dt.datetime.now(TZ).strftime("%Y%m%d-%H%M%S") + "-" + uuid.uuid4().hex[:6])
            report = {"started_at": now(), "source": "F2 user.aweme_count", "f2_commit_expected": F2_COMMIT, "results": []}
            jar = await context.cookies(["https://www.douyin.com/"])
            cookies = {c["name"]: c["value"] for c in jar}
            logged_in = bool(cookies.get("sessionid") or cookies.get("sessionid_ss"))
            headers = ClientConfManager.headers().copy()
            headers["User-Agent"] = await page.evaluate("navigator.userAgent")
            cfg = {"cookie": "; ".join(k + "=" + v for k, v in cookies.items()), "headers": headers, "timeout": args.timeout, "max_retries": 1, "max_tasks": 1}
            streak = 0
            async with DouyinCrawler(cfg) as client:
                for index, item in enumerate(items):
                    row = {"url": item["url"], "account": item["expected_account"] or "", "count": None, "observed_at": now()}
                    if streak >= args.max_failures:
                        row.update(status="skipped", error="ConsecutiveFailureLimit")
                    else:
                        if index:
                            await asyncio.sleep(args.interval)
                        try:
                            if not logged_in:
                                raise LoginRequired()
                            params = UserProfile(sec_user_id=item["sec_uid"], msToken=cookies.get("msToken", ""))
                            data = await asyncio.wait_for(client.fetch_user_profile(params), timeout=args.timeout * 3)
                            row.update(parse_user(data, item), status="success", error="")
                            streak = 0
                        except Exception as exc:
                            row.update(status="failed", error=type(exc).__name__)
                            streak += 1
                    row["observed_at"] = now()
                    report["results"].append(row)
                    write_results(folder, report)
                    print(json.dumps({k: row.get(k) for k in ("account", "count", "status", "error")}, ensure_ascii=True), flush=True)
            report["finished_at"] = now()
            write_results(folder, report)
            print("Report: " + str(folder / "results.html"), flush=True)
            return 0 if all(r["status"] == "success" for r in report["results"]) else 2
        finally:
            await context.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    for name in ("login", "query"):
        command = sub.add_parser(name)
        command.add_argument("--profile", type=Path, required=True, help="Dedicated local browser profile; do not share")
        command.add_argument("--browser-executable", type=Path)
        if name == "query":
            command.add_argument("--input", type=Path, required=True)
            command.add_argument("--output", type=Path, required=True)
            command.add_argument("--interval", type=float, default=3)
            command.add_argument("--timeout", type=float, default=20)
            command.add_argument("--max-failures", type=int, default=3)
    args = parser.parse_args()
    if args.command == "query" and (args.interval < 0 or args.timeout <= 0 or args.max_failures < 1):
        parser.error("Require interval >= 0, timeout > 0 and max-failures >= 1")
    try:
        return asyncio.run(run(args))
    except KeyboardInterrupt:
        print("Interrupted; any already written results are retained.", file=sys.stderr)
        return 130
    except Exception as exc:
        # Never print upstream exceptions, which can contain signed URLs or credentials.
        message = str(exc) if isinstance(exc, (ValueError, FileNotFoundError)) else "Check setup, browser availability and profile lock"
        print(type(exc).__name__ + ": " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
