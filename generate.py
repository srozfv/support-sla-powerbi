"""Build a portable Power BI import model and a deterministic CSV snapshot."""
import base64
import csv
import json
import zlib
from datetime import datetime, timedelta
from pathlib import Path
from random import Random

ROOT = Path(__file__).resolve().parent
MODEL = ROOT / "SupportSLA.SemanticModel" / "definition"
START = datetime(2025, 7, 1)
AS_OF = datetime(2026, 1, 1)
QUEUES = [(1, "Доступы"), (2, "Рабочее место"), (3, "Сеть"), (4, "Биллинг")]
PRIORITIES = [(1, "P1", 60), (2, "P2", 240), (3, "P3", 1440)]

# (Power BI field, TOM type, Power Query type)
SCHEMA = {
    "Tickets": [
        ("TicketId", "int64", "Int64.Type"), ("CreatedAt", "dateTime", "type datetime"),
        ("CreatedDate", "dateTime", "type date"), ("QueueId", "int64", "Int64.Type"),
        ("PriorityId", "int64", "Int64.Type"), ("DeadlineAt", "dateTime", "type datetime"),
        ("FirstReplyAt", "dateTime", "type datetime"), ("ReplyMinutes", "int64", "Int64.Type"),
        ("Mature", "boolean", "type logical"), ("OnTime", "boolean", "type logical"),
        ("OpenOverdue", "boolean", "type logical"),
    ],
    "Calendar": [("Date", "dateTime", "type date"), ("MonthKey", "string", "type text")],
    "Queues": [("QueueId", "int64", "Int64.Type"), ("Queue", "string", "type text")],
    "Priorities": [("PriorityId", "int64", "Int64.Type"), ("Priority", "string", "type text"),
                   ("TargetMinutes", "int64", "Int64.Type")],
}

MEASURES = {
    "Обращения": ("COALESCE(COUNTROWS(Tickets), 0)", "#,0"),
    "В расчёте SLA": ("CALCULATE([Обращения], KEEPFILTERS(Tickets[Mature] = TRUE()))", "#,0"),
    "В срок": ("CALCULATE([В расчёте SLA], KEEPFILTERS(Tickets[OnTime] = TRUE()))", "#,0"),
    "SLA первого ответа": ("DIVIDE([В срок], [В расчёте SLA])", "0.0%"),
    "Нарушения SLA": ("[В расчёте SLA] - [В срок]", "#,0"),
    "Просрочено без ответа": ("CALCULATE([Обращения], KEEPFILTERS(Tickets[OpenOverdue] = TRUE()))", "#,0"),
    "Медиана ответа мин": (
        "CALCULATE(MEDIAN(Tickets[ReplyMinutes]), KEEPFILTERS(Tickets[Mature] = TRUE()), "
        "KEEPFILTERS(NOT ISBLANK(Tickets[ReplyMinutes])))", "0.0"
    ),
}


def classify(created, target, reply):
    deadline = created + timedelta(minutes=target)
    observed = reply if reply is not None and reply <= AS_OF else None
    minutes = (observed - created) // timedelta(minutes=1) if observed is not None else None
    mature = deadline <= AS_OF
    on_time = mature and observed is not None and observed <= deadline
    overdue = mature and observed is None
    return deadline, observed, minutes, mature, on_time, overdue


def fixtures():
    yield 1, AS_OF - timedelta(minutes=60), 1, 1, AS_OF
    yield 2, AS_OF - timedelta(minutes=59), 1, 1, None
    yield 3, AS_OF - timedelta(minutes=60), 1, 1, AS_OF + timedelta(minutes=1)
    yield 4, START, 1, 1, START + timedelta(minutes=61)
    yield 5, AS_OF - timedelta(minutes=59), 1, 1, AS_OF - timedelta(minutes=30)
    yield 6, START, 1, 1, START


def tickets():
    rng = Random(73241)
    records = list(fixtures())
    for ticket_id in range(7, 4801):
        queue, = rng.choices([1, 2, 3, 4], weights=[32, 31, 25, 12])
        priority, = rng.choices([1, 2, 3], weights=[11, 42, 47])
        created = START + timedelta(minutes=rng.randrange((AS_OF - START) // timedelta(minutes=1)))
        target = PRIORITIES[priority - 1][2]
        typical = {1: 0.30, 2: 0.47, 3: 0.72, 4: 0.38}[queue]
        if queue == 3 and created.month >= 11:
            typical *= 1.45
        elapsed = round(target * rng.lognormvariate(0, 0.82) * typical)
        proposed = created + timedelta(minutes=elapsed)
        records.append((ticket_id, created, queue, priority, proposed if rng.random() >= 0.035 else None))
    return [
        (i, created, created.date(), q, p, *classify(created, PRIORITIES[p - 1][2], proposed))
        for i, created, q, p, proposed in records
    ]


def payload(rows):
    raw = json.dumps(rows, default=lambda d: d.isoformat(), ensure_ascii=False,
                     separators=(",", ":")).encode("utf-8")
    compressor = zlib.compressobj(wbits=-15)
    return base64.b64encode(compressor.compress(raw) + compressor.flush()).decode("ascii")


def table_tmdl(name, rows):
    fields = SCHEMA[name]
    text = [f"table {name}"]
    if name == "Tickets":
        for measure, (formula, fmt) in MEASURES.items():
            text.extend(["", f"\tmeasure '{measure}' = {formula}", f"\t\tformatString: {fmt}"])
    for field, tom, _ in fields:
        text.extend(["", f"\tcolumn {field}", f"\t\tdataType: {tom}"])
        if field.endswith("Id") or tom == "boolean" or field in ("ReplyMinutes", "DeadlineAt"):
            text.append("\t\tisHidden")
        if tom == "dateTime":
            text.append("\t\tformatString: " + ("yyyy-MM-dd HH:mm" if field.endswith("At") else "yyyy-MM-dd"))
        text.extend(["\t\tsummarizeBy: none", f"\t\tsourceColumn: {field}"])
    names = ", ".join(f'"{field}"' for field, _, _ in fields)
    types = ", ".join(f'{{"{field}", {m}}}' for field, _, m in fields)
    text.extend([
        "", f"\tpartition {name} = m", "\t\tmode: import", "\t\tsource =",
        "\t\t\t\tlet",
        f'\t\t\t\t    Packed = "{payload(rows)}",',
        '\t\t\t\t    Bytes = Binary.Decompress(Binary.FromText(Packed, BinaryEncoding.Base64), Compression.Deflate),',
        f"\t\t\t\t    Raw = #table({{{names}}}, Json.Document(Bytes)),",
        f'\t\t\t\t    Typed = Table.TransformColumnTypes(Raw, {{{types}}}, "en-US")',
        "\t\t\t\tin", "\t\t\t\t    Typed", "",
    ])
    return "\n".join(text)


def write(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")


def main():
    days = [START.date() + timedelta(days=i) for i in range((AS_OF - START).days)]
    tables = {"Tickets": tickets(), "Calendar": [(d, d.strftime("%Y-%m")) for d in days],
              "Queues": QUEUES, "Priorities": PRIORITIES}
    for name, rows in tables.items():
        path = ROOT / "data" / f"{name}.csv"
        path.parent.mkdir(exist_ok=True)
        with path.open("w", newline="", encoding="utf-8") as file:
            writer = csv.writer(file, lineterminator="\n")
            writer.writerow([field for field, _, _ in SCHEMA[name]])
            writer.writerows(rows)
        write(MODEL / "tables" / f"{name}.tmdl", table_tmdl(name, rows))
    query = "EVALUATE\nROW(\n" + ",\n".join(f'    "{name}", [{name}]' for name in MEASURES) + "\n)\n"
    query += "\nEVALUATE\nSUMMARIZECOLUMNS(\n    'Queues'[Queue],\n    'Calendar'[MonthKey],\n"
    query += ",\n".join(f'    "{name}", [{name}]' for name in MEASURES) + "\n)\nORDER BY 'Queues'[Queue], 'Calendar'[MonthKey]\n"
    write(ROOT / "checks.dax", query)


if __name__ == "__main__":
    main()
