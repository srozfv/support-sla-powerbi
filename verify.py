"""Independently recompute KPIs from timestamp fields and check embedded data."""
import argparse
import base64
import csv
import json
import math
import re
import statistics
import unittest
import zlib
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / "SupportSLA.SemanticModel" / "definition" / "tables"
SNAPSHOT = datetime(2026, 1, 1)


def read(name):
    with (ROOT / "data" / f"{name}.csv").open(encoding="utf-8", newline="") as source:
        return list(csv.DictReader(source))


def metrics(rows):
    targets = {p["PriorityId"]: int(p["TargetMinutes"]) for p in read("Priorities")}
    eligible, met, waiting = 0, 0, 0
    minutes = []
    for row in rows:
        created = datetime.fromisoformat(row["CreatedAt"])
        reply = datetime.fromisoformat(row["FirstReplyAt"]) if row["FirstReplyAt"] else None
        elapsed_at_snapshot = (SNAPSHOT - created).total_seconds() / 60
        target = targets[row["PriorityId"]]
        if elapsed_at_snapshot >= target:
            eligible += 1
            if reply is None:
                waiting += 1
            else:
                elapsed = (reply - created).total_seconds() / 60
                minutes.append(elapsed)
                met += elapsed <= target
    return [len(rows), eligible, met, met / eligible if eligible else None,
            eligible - met, waiting, statistics.median(minutes) if minutes else None]


def groups():
    tickets = read("Tickets")
    result = {"all": metrics(tickets), "empty": metrics([])}
    for queue in read("Queues"):
        key = queue["QueueId"]
        for month in ("2025-07", "2025-08", "2025-09", "2025-10", "2025-11", "2025-12"):
            result[f'{queue["Queue"]} / {month}'] = metrics(
                [row for row in tickets if row["QueueId"] == key and row["CreatedDate"].startswith(month)]
            )
        result[queue["Queue"]] = metrics([row for row in tickets if row["QueueId"] == key])
    for priority in read("Priorities"):
        key = priority["PriorityId"]
        result[priority["Priority"]] = metrics([row for row in tickets if row["PriorityId"] == key])
    return result


class ModelChecks(unittest.TestCase):
    def test_csv_matches_power_query_payload(self):
        for name in ("Tickets", "Calendar", "Queues", "Priorities"):
            with self.subTest(name=name):
                source = (MODEL / f"{name}.tmdl").read_text(encoding="utf-8")
                encoded = re.search(r'Packed = "([^"]+)"', source)[1]
                payload = json.loads(zlib.decompress(base64.b64decode(encoded), wbits=-15))
                normalized = [
                    [str(value).replace("T", " ") if isinstance(value, str) and re.match(r"\d{4}-\d\d-\d\dT", value)
                     else "" if value is None else str(value) for value in row]
                    for row in payload
                ]
                self.assertEqual(normalized, [list(row.values()) for row in read(name)])

    def test_integrity_and_rules(self):
        tickets = read("Tickets")
        self.assertEqual(len({row["TicketId"] for row in tickets}), len(tickets))
        for table, column in [("Calendar", "Date"), ("Queues", "QueueId"), ("Priorities", "PriorityId")]:
            keys = [r[column] for r in read(table)]
            fk = "CreatedDate" if column == "Date" else column
            self.assertEqual(len(keys), len(set(keys)))
            self.assertTrue({r[fk] for r in tickets} <= set(keys))
        thresholds = {r["PriorityId"]: int(r["TargetMinutes"]) for r in read("Priorities")}
        for row in tickets:
            created = datetime.fromisoformat(row["CreatedAt"])
            deadline = datetime.fromisoformat(row["DeadlineAt"])
            reply = datetime.fromisoformat(row["FirstReplyAt"]) if row["FirstReplyAt"] else None
            self.assertEqual(str(created.date()), row["CreatedDate"])
            self.assertGreater(deadline, created)
            self.assertLess(created, SNAPSHOT)
            self.assertEqual(deadline - created, timedelta(minutes=thresholds[row["PriorityId"]]))
            if reply is not None:
                self.assertTrue(created <= reply <= SNAPSHOT)
                self.assertEqual((reply - created).total_seconds(), int(row["ReplyMinutes"]) * 60)
            else:
                self.assertEqual(row["ReplyMinutes"], "")
            self.assertEqual(row["Mature"], str(deadline <= SNAPSHOT))
            self.assertEqual(row["OnTime"], str(deadline <= SNAPSHOT and reply is not None and reply <= deadline))
            self.assertEqual(row["OpenOverdue"], str(deadline <= SNAPSHOT and reply is None))

    def test_exact_threshold_and_snapshot(self):
        flags = [(r["Mature"], r["OnTime"], r["OpenOverdue"], r["ReplyMinutes"]) for r in read("Tickets")[:6]]
        self.assertEqual(flags, [
            ("True", "True", "False", "60"), ("False", "False", "False", ""),
            ("True", "False", "True", ""), ("True", "False", "False", "61"),
            ("False", "False", "False", "29"), ("True", "True", "False", "0"),
        ])

    def test_reference_metrics(self):
        reference = json.loads((ROOT / "expected.json").read_text(encoding="utf-8"))
        self.assertEqual(groups(), reference)
        for total, eligible, met, ratio, missed, backlog, _ in reference.values():
            self.assertTrue(0 <= backlog <= missed <= eligible <= total)
            self.assertEqual(met + missed, eligible)
            self.assertTrue(math.isclose(ratio, met / eligible) if eligible else ratio is None)

    def test_visual_bindings_and_layout(self):
        pages = ROOT / "SupportSLA.Report" / "definition" / "pages"
        meta = json.loads((pages / "pages.json").read_text(encoding="utf-8"))
        self.assertIn(meta["activePageName"], meta["pageOrder"])
        model = {p.stem: p.read_text(encoding="utf-8") for p in MODEL.glob("*.tmdl")}
        for page in meta["pageOrder"]:
            bounds = json.loads((pages / page / "page.json").read_text(encoding="utf-8"))
            visuals = list((pages / page / "visuals").glob("*/visual.json"))
            self.assertGreaterEqual(len(visuals), 7)
            for file in visuals:
                item = json.loads(file.read_text(encoding="utf-8"))
                self.assertEqual(file.parent.name, item["name"])
                pos = item["position"]
                self.assertLessEqual(pos["x"] + pos["width"], bounds["width"])
                self.assertLessEqual(pos["y"] + pos["height"], bounds["height"])
                for slot in item["visual"].get("query", {}).get("queryState", {}).values():
                    for projection in slot["projections"]:
                        kind, expr = next(iter(projection["field"].items()))
                        entity = expr["Expression"]["SourceRef"]["Entity"]
                        prop = expr["Property"]
                        token = f"column {prop}\n" if kind == "Column" else f"measure '{prop}' ="
                        self.assertIn(token, model[entity])


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--write-reference", action="store_true")
    args = parser.parse_args()
    if args.write_reference:
        (ROOT / "expected.json").write_text(json.dumps(groups(), ensure_ascii=False, indent=2) + "\n",
                                           encoding="utf-8")
    else:
        unittest.main(argv=[__file__], verbosity=2)
