"""Verify that the local collector resumes a batch without losing verified results."""
import csv
from contextlib import redirect_stderr
import io
import json
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from collect_counts_batch import BatchState, collect_pending, recover_attempts
from collection_profiles import ProfileIndex
from prepare_collection_profiles import build_index, main as prepare_index, verified_rows

SEEN_AT = "2026-11-20T12:00:00+08:00"
URL = "https://www.douyin.com/user/MS4wSynthetic"


def success(account, count):
    return {"account": account, "url": URL + account, "count": count,
            "observed_at": SEEN_AT, "status": "success"}


class CollectCountsBatchTest(unittest.TestCase):
    def test_browser_success_report_contributes_identity_without_copying_count(self):
        with tempfile.TemporaryDirectory() as folder:
            source = Path(folder) / "verified.json"
            source.write_text(json.dumps({"completed_at": SEEN_AT, "source": "抖音网页版公开主页",
                "success": 1, "failed": 0, "skipped": 0,
                "results": [{"account":"001","url":URL + "001","posts":"0","followers":"3"}]}))
            self.assertEqual(verified_rows(source), [
                {"account":"001","url":URL + "001","status":"已核实"}])

    def test_conflicting_report_cannot_replace_output_index(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            accounts, source, base, output = (root / name for name in
                                           ("accounts.json","source.json","base.json","output.json"))
            accounts.write_text(json.dumps(["001"]))
            source.write_text(json.dumps({"results":[{"account":"001","url":URL + "Other","status":"已核实"}]}))
            base.write_text(json.dumps({"version":1,"profiles":[{"account":"001","url":URL + "001"}]}))
            base.chmod(0o600)
            output.write_text("existing")
            args = ["prepare_collection_profiles.py","--accounts",str(accounts),"--verified-report",str(source),
                    "--base-index",str(base),"--output",str(output)]
            with patch("sys.argv",args),redirect_stderr(io.StringIO()) as err,self.assertRaises(SystemExit) as stop:
                prepare_index()
            self.assertEqual(stop.exception.code,2)
            self.assertIn("主页归属冲突",err.getvalue())
            self.assertEqual(output.read_text(),"existing")

    def test_import_only_exact_verified_rows_and_report_conflicts(self):
        accounts = ["001", "002", "003", "004", "005", "006"]
        base = [{"account": "001", "url": URL + "001"},
                {"account": "003", "url": URL + "003"}]
        verified = [{"account": "002", "url": URL + "002", "status": "已核实"},
                    {"account": "003", "url": URL + "Other", "status": "已核实"},
                    {"account": "004", "url": URL + "004", "status": "跳过"},
                    {"account": "005", "url": URL + "same", "status": "已核实"},
                    {"account": "006", "url": URL + "same", "status": "已核实"},
                    {"account": "OUTSIDE", "url": URL + "outside", "status": "已核实"}]
        result, summary = build_index(accounts, base, verified)
        self.assertEqual(result, {"version": 1, "profiles": [
            {"account": "001", "url": URL + "001"},
            {"account": "002", "url": URL + "002"},
        ]})
        self.assertEqual(summary, {"targets": 6, "previously_mapped": 2, "ready": 2,
                                   "missing": 1, "conflicts": 3})

    def test_recover_committed_subprocess_result_after_parent_stops(self):
        accounts = ["001", "002"]
        with tempfile.TemporaryDirectory() as folder:
            output = Path(folder)
            partial = output / "search-synthetic"
            report = partial / "synthetic-run" / "results.json"
            report.parent.mkdir(parents=True)
            (partial / "accounts.json").write_text(json.dumps([{"account": a} for a in accounts]))
            report.write_text(json.dumps({"results": [success("001", 4)]}))
            state = BatchState(accounts)
            recover_attempts(output, accounts, state)
            self.assertEqual(state.pending_handles(), ["002"])
            self.assertEqual(state.success("001")["count"], 4)

    def test_checkpoint_round_trip_then_complete_only_pending_ids(self):
        handles = ["001", "002", "003"]
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            profile = root / "profile"
            profile.mkdir()
            args = SimpleNamespace(output=root, profile=profile, search_skill=root / "synthetic-skill.py")
            state = BatchState(handles)
            index = ProfileIndex({"001": {URL + "001"}})
            attempts = []

            def first_run(_, script, items, label):
                attempts.append((label, [item["account"] for item in items]))
                if label == "known":
                    self.assertEqual(items, [{"account": "001", "url": URL + "001"}])
                    return [success("001", 0)], None
                self.assertEqual(items, [{"account": "002"}, {"account": "003"}])
                return [success("002", 7),
                        {"account": "003", "status": "failed", "error": "SearchNeedsVerification"}], "SearchNeedsVerification"

            self.assertEqual(collect_pending(args, state, index, first_run), "SearchNeedsVerification")
            self.assertEqual(attempts, [("known", ["001"]), ("search", ["002", "003"])])
            restored = BatchState.open(root / "checkpoint.json", handles)
            self.assertEqual(restored.pending_handles(), ["003"])
            with (root / "作品采集结果.csv").open(encoding="utf-8-sig", newline="") as stream:
                counts = {row["完整抖音号"]: row["作品数"] for row in csv.DictReader(stream)}
            self.assertEqual(counts, {"001": "0", "002": "7", "003": ""})

            def second_run(_, script, items, label):
                self.assertEqual(label, "search")
                self.assertEqual(items, [{"account": "003"}])
                return [success("003", 2)], None

            self.assertIsNone(collect_pending(args, restored, index, second_run))
            self.assertEqual(restored.pending_handles(), [])
            self.assertEqual(restored.success("001")["observed_at"], SEEN_AT)
