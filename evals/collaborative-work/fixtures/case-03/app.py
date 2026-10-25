import json


SAMPLE_JSON = '''[
  {"name": "林青", "team": "前台", "hours": 3.5, "note": "午后, 可调班"},
  {"name": "Chen", "team": "后勤", "hours": 2, "note": "带\\\"备用\\\"标签"},
  {"name": "王宁", "team": "前台", "hours": 0, "note": ""}
]'''


def load_rows(text):
    return json.loads(text)


def visible_rows(rows, team=None):
    if not team:
        return list(rows)
    return [row for row in rows if row["team"] == team]


def summary(rows):
    return {
        "count": len(rows),
        "total_hours": sum(row["hours"] for row in rows),
    }
