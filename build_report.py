"""Write the native Power BI report layout and project metadata."""
import json
from generate import ROOT, MODEL, SCHEMA, write

SCHEMAS = "https://developer.microsoft.com/json-schemas/fabric/"
REPORT = ROOT / "SupportSLA.Report"
DEF = REPORT / "definition"


def document(path, schema, **fields):
    write(path, json.dumps({"$schema": SCHEMAS + schema + "/schema.json", **fields},
                           ensure_ascii=False, indent=2) + "\n")


def prop(value):
    if isinstance(value, bool):
        literal = str(value).lower()
    elif isinstance(value, int):
        literal = f"{value}D"
    else:
        literal = "'" + value.replace("'", "''") + "'"
    return {"expr": {"Literal": {"Value": literal}}}


def color(value):
    return {"solid": {"color": prop(value)}}


def obj(**properties):
    return [{"properties": properties}]


def field(table, name, label=None):
    kind = "Column" if name in {col[0] for col in SCHEMA[table]} else "Measure"
    data = {
        "field": {kind: {"Expression": {"SourceRef": {"Entity": table}}, "Property": name}},
        "queryRef": table + "." + name, "nativeQueryRef": name,
    }
    if kind == "Column":
        data["active"] = True
    if label:
        data["displayName"] = label
    return data


def visual(page, name, kind, bounds, title=None, query=None, objects=None, sort=None):
    x, y, width, height, order = bounds
    panel = {"visualType": kind}
    if query:
        panel["query"] = {"queryState": {role: {"projections": fields} for role, fields in query.items()}}
    if sort:
        by, direction = sort
        panel["query"]["sortDefinition"] = {"sort": [{"field": by["field"], "direction": direction}],
                                            "isDefaultSort": True}
    if kind == "cardVisual":
        panel["objects"] = {
            "value": [{"selector": {"id": "default"}, "properties": {
                "labelDisplayUnits": prop(-1), "fontSize": prop(30), "fontColor": color("#142B42"),
                "fontFamily": prop("Segoe UI Semibold"), "showBlankAs": prop("—"),
            }}],
            "label": [{"selector": {"id": "default"}, "properties": {
                "show": prop(True), "position": prop("aboveValue"), "fontSize": prop(13),
                "fontColor": color("#415D76"),
            }}],
        }
    if objects:
        panel["objects"] = objects
    panel["visualContainerObjects"] = {
        "background": obj(show=prop(kind != "textbox"), color=color("#FFFFFF"), transparency=prop(0))
    }
    if title:
        panel["visualContainerObjects"]["title"] = obj(show=prop(True), text=prop(title),
                                                        fontSize=prop(13), fontColor=color("#142B42"))
    if kind in ("lineChart", "clusteredBarChart", "tableEx"):
        panel["drillFilterOtherVisuals"] = True
    document(DEF / "pages" / page / "visuals" / name / "visual.json",
             "item/report/definition/visualContainer/2.1.0", name=name,
             position=dict(x=x, y=y, width=width, height=height, z=order * 10, tabOrder=order),
             visual=panel)


def textbox(page, name, bounds, lines):
    paragraphs = [
        {"textRuns": [{"value": line, "textStyle": {"fontFamily": "Segoe UI",
             "fontSize": f"{size}px", "color": ink, "fontWeight": weight}}],
         "horizontalTextAlignment": "left"}
        for line, size, ink, weight in lines
    ]
    visual(page, name, "textbox", bounds, objects={"general": obj(paragraphs=paragraphs)})


def page_header(name, heading, subtitle):
    document(DEF / "pages" / name / "page.json", "item/report/definition/page/2.0.0",
             name=name, displayName=heading, displayOption="FitToPage", width=1280, height=720,
             objects={"background": obj(color=color("#EEF3F8"), transparency=prop(0))})
    textbox(name, "heading", (20, 12, 1240, 73, 1), [
        (heading, 28, "#142B42", "bold"), (subtitle, 14, "#415D76", "normal")
    ])


def filters(page, third):
    for index, (table, field_name, label) in enumerate(
        [("Calendar", "MonthKey", "Месяц создания"), ("Queues", "Queue", "Очередь"), third]
    ):
        visual(page, ["month", "queue", "priority"][index], "slicer",
               (24 + index * 416, 90, 400, 78, index + 2),
               query={"Values": [field(table, field_name, label)]},
               objects={"data": obj(mode=prop("Dropdown")), "header": obj(show=prop(True), text=prop(label))})


def main():
    document(ROOT / "SupportSLA.pbip", "pbip/pbipProperties/1.0.0",
             version="1.0", artifacts=[{"report": {"path": "SupportSLA.Report"}}],
             settings={"enableAutoRecovery": True})
    document(REPORT / "definition.pbir", "item/report/definitionProperties/2.0.0",
             version="4.0", datasetReference={"byPath": {"path": "../SupportSLA.SemanticModel"}})
    document(MODEL.parent / "definition.pbism", "item/semanticModel/definitionProperties/1.0.0",
             version="4.0", settings={"qnaEnabled": False})
    document(DEF / "version.json", "item/report/definition/versionMetadata/1.0.0", version="2.0.0")
    document(DEF / "report.json", "item/report/definition/report/2.1.0",
             themeCollection={"baseTheme": {"name": "CY24SU10", "reportVersionAtImport": "5.65",
                                            "type": "SharedResources"}})
    document(DEF / "pages" / "pages.json", "item/report/definition/pagesMetadata/1.0.0",
             pageOrder=["overview", "overdue"], activePageName="overview")

    priority = ("Priorities", "Priority", "Приоритет")
    month = field("Calendar", "MonthKey", "Месяц")
    queue = field("Queues", "Queue", "Очередь")
    coverage = field("Tickets", "SLA первого ответа")
    breaches = field("Tickets", "Нарушения SLA")
    backlog = field("Tickets", "Просрочено без ответа")
    page_header("overview", "SLA первого ответа", "Синтетические данные · июль–декабрь 2025 · срез на 01.01.2026 00:00 UTC")
    filters("overview", priority)
    for i, (name, metric) in enumerate([("received", "Обращения"), ("sla", "SLA первого ответа"),
                                       ("breaches", "Нарушения SLA"), ("backlog", "Просрочено без ответа")]):
        visual("overview", name, "cardVisual", (24 + i * 312, 184, 296, 112, 5 + i),
               query={"Data": [field("Tickets", metric)]})
    visual("overview", "trend", "lineChart", (24, 312, 608, 218, 9), "Динамика SLA по месяцу создания",
           {"Category": [month], "Y": [coverage]}, sort=(month, "Ascending"))
    visual("overview", "breach_by_queue", "clusteredBarChart", (648, 312, 608, 218, 10),
           "Нарушения SLA по очередям", {"Category": [queue], "Y": [breaches]}, sort=(breaches, "Descending"))
    visual("overview", "summary", "tableEx", (24, 546, 1232, 154, 11), "Сводка по очередям",
           {"Values": [queue, field("Tickets", "В расчёте SLA"), field("Tickets", "В срок"),
                       coverage, breaches, backlog, field("Tickets", "Медиана ответа мин")]},
           sort=(breaches, "Descending"))

    page_header("overdue", "Просрочено без ответа", "Синтетические данные · заявки без первого ответа на 01.01.2026 00:00 UTC")
    filters("overdue", priority)
    visual("overdue", "current", "cardVisual", (24, 184, 300, 112, 5), query={"Data": [backlog]})
    visual("overdue", "distribution", "clusteredBarChart", (340, 184, 916, 210, 6),
           "Просроченные обращения по очередям", {"Category": [queue], "Y": [backlog]},
           sort=(backlog, "Descending"))
    table = [field("Tickets", "TicketId", "ID"), queue, field("Priorities", "Priority", "Приоритет"),
             field("Tickets", "CreatedAt", "Создано UTC"), field("Tickets", "DeadlineAt", "Срок UTC"), backlog]
    visual("overdue", "tickets", "tableEx", (24, 410, 1232, 290, 7), "Очередь разбора",
           {"Values": table}, sort=(table[4], "Ascending"))
    # Filter on the per-ticket measure. Zero is a valid KPI elsewhere and is hidden only in this table.
    path = DEF / "pages/overdue/visuals/tickets/visual.json"
    layout = json.loads(path.read_text(encoding="utf-8"))
    layout["filterConfig"] = {"filters": [{
        "name": "waiting_only", "field": backlog["field"], "type": "Advanced",
        "filter": {"Version": 2, "From": [{"Name": "t", "Entity": "Tickets", "Type": 0}],
                   "Where": [{"Condition": {"Comparison": {"ComparisonKind": 1,
                       "Left": {"Measure": {"Expression": {"SourceRef": {"Source": "t"}},
                                            "Property": "Просрочено без ответа"}},
                       "Right": {"Literal": {"Value": "0L"}}}}}]}
    }]}
    write(path, json.dumps(layout, ensure_ascii=False, indent=2) + "\n")


if __name__ == "__main__":
    main()
