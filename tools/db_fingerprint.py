"""Fingerprint a SQLite database so two copies can be compared row-for-row.

Opens the file read-only and records, for every table, the row count and a
SHA-256 of all rows (ordered by rowid). Runs without Django so it can check a
database before and after an upgrade with exactly the same code.

    python tools/db_fingerprint.py snapshot db.sqlite3 before.json
    python tools/db_fingerprint.py compare before.json after.json
    python tools/db_fingerprint.py diff old.sqlite3 new.sqlite3
"""
import hashlib
import json
import sqlite3
import sys


def snapshot(db_path):
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    result = {"integrity": conn.execute("PRAGMA integrity_check").fetchone()[0], "tables": {}}
    for table in tables:
        cols = [r[1] for r in conn.execute(f'PRAGMA table_info("{table}")')]
        digest = hashlib.sha256()
        count = 0
        # Hash by column name so a rebuilt table with reordered columns still matches.
        order = sorted(cols)
        select = ", ".join(f'"{c}"' for c in order)
        for row in conn.execute(f'SELECT {select} FROM "{table}" ORDER BY rowid'):
            digest.update(repr(row).encode("utf-8"))
            count += 1
        result["tables"][table] = {"rows": count, "sha256": digest.hexdigest(), "columns": order}
    conn.close()
    return result


def compare(before, after):
    ok = True
    print(f"integrity: before={before['integrity']} after={after['integrity']}")
    if after["integrity"] != "ok":
        ok = False
    names = sorted(set(before["tables"]) | set(after["tables"]))
    for name in names:
        b, a = before["tables"].get(name), after["tables"].get(name)
        if b is None:
            print(f"  NEW      {name}: {a['rows']} rows")
        elif a is None:
            print(f"  MISSING  {name}: had {b['rows']} rows")
            ok = False
        elif b["sha256"] == a["sha256"]:
            print(f"  same     {name}: {a['rows']} rows")
        else:
            cols = ""
            if b["columns"] != a["columns"]:
                added = sorted(set(a["columns"]) - set(b["columns"]))
                removed = sorted(set(b["columns"]) - set(a["columns"]))
                cols = f" columns +{added} -{removed}"
            print(f"  CHANGED  {name}: {b['rows']} -> {a['rows']} rows{cols}")
            if a["rows"] < b["rows"] or (b["columns"] != a["columns"] and set(b["columns"]) - set(a["columns"])):
                ok = False
    return ok


def diff(old_path, new_path):
    """Row-level comparison of two database files on the columns they share.

    Rows are matched by `id` (or rowid). Reports, per table, which rows were
    added, removed or changed, and which columns changed, so a migration's
    effect on data can be checked exactly.
    """
    old = sqlite3.connect(f"file:{old_path}?mode=ro", uri=True)
    new = sqlite3.connect(f"file:{new_path}?mode=ro", uri=True)
    list_tables = ("SELECT name FROM sqlite_master WHERE type='table' "
                   "AND name NOT LIKE 'sqlite_%' ORDER BY name")
    old_tables = [r[0] for r in old.execute(list_tables)]
    new_tables = {r[0] for r in new.execute(list_tables)}
    report = {}
    for table in old_tables:
        if table not in new_tables:
            report[table] = {"missing": True}
            continue
        old_cols = [r[1] for r in old.execute(f'PRAGMA table_info("{table}")')]
        new_cols = [r[1] for r in new.execute(f'PRAGMA table_info("{table}")')]
        shared = [c for c in old_cols if c in new_cols]
        key = "id" if "id" in shared else "rowid"
        cols = ", ".join(f'"{c}"' for c in shared)

        def rows(conn):
            return {r[0]: r[1:] for r in conn.execute(f'SELECT {key}, {cols} FROM "{table}"')}

        a, b = rows(old), rows(new)
        removed = sorted(set(a) - set(b))
        added = sorted(set(b) - set(a))
        changed = sorted(k for k in set(a) & set(b) if a[k] != b[k])
        changed_cols = sorted({shared[i] for k in changed for i, (x, y) in enumerate(zip(a[k], b[k])) if x != y})
        report[table] = {
            "rows_old": len(a), "rows_new": len(b),
            "removed": removed, "added": added, "changed": changed,
            "changed_columns": changed_cols,
            "new_columns": [c for c in new_cols if c not in old_cols],
            "dropped_columns": [c for c in old_cols if c not in new_cols],
        }
    report["_new_tables"] = sorted(new_tables - set(old_tables))
    old.close()
    new.close()
    return report


def print_diff(report):
    for table, r in report.items():
        if table == "_new_tables":
            continue
        if r.get("missing"):
            print(f"  MISSING  {table}")
            continue
        flags = []
        if r["removed"]:
            flags.append(f"{len(r['removed'])} removed (ids {r['removed'][:10]})")
        if r["added"]:
            flags.append(f"{len(r['added'])} added")
        if r["changed"]:
            flags.append(f"{len(r['changed'])} changed in {r['changed_columns']} (ids {r['changed'][:20]})")
        if r["new_columns"]:
            flags.append(f"new columns {r['new_columns']}")
        if r["dropped_columns"]:
            flags.append(f"DROPPED columns {r['dropped_columns']}")
        status = "same    " if not flags else "DIFF    "
        print(f"  {status} {table}: {r['rows_old']} -> {r['rows_new']} rows" + ("; " + "; ".join(flags) if flags else ""))
    if report["_new_tables"]:
        print(f"  new tables: {report['_new_tables']}")


if __name__ == "__main__":
    if len(sys.argv) == 4 and sys.argv[1] == "diff":
        print_diff(diff(sys.argv[2], sys.argv[3]))
    elif len(sys.argv) == 4 and sys.argv[1] == "snapshot":
        with open(sys.argv[3], "w", encoding="utf-8") as f:
            json.dump(snapshot(sys.argv[2]), f, indent=1)
        print(f"wrote {sys.argv[3]}")
    elif len(sys.argv) == 4 and sys.argv[1] == "compare":
        with open(sys.argv[2], encoding="utf-8") as f:
            before = json.load(f)
        with open(sys.argv[3], encoding="utf-8") as f:
            after = json.load(f)
        sys.exit(0 if compare(before, after) else 1)
    else:
        print(__doc__)
        sys.exit(2)
