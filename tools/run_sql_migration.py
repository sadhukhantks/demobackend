"""Run a dbqueries/migrations/*.sql script against a tenant DB via pymysql.

Comment-safe: strips `-- ...` line comments and `/* ... */` blocks BEFORE splitting
on ';', so a statement preceded by a comment block is not silently swallowed (the
failure mode that half-applied the govtskg and waste-entry migrations).

Usage:
    python tools/run_sql_migration.py <db_name> <path/to/script.sql> [--dry-run]
"""

import sys
from pathlib import Path

import pymysql
from dotenv import dotenv_values


def split_statements(sql: str) -> list[str]:
    # Character scanner instead of regex+split: a `;`, `--` or `/*` inside a
    # quoted literal ('...', "...", `...`) must not terminate/strip anything —
    # regex split broke on COMMENT 'override; ...' (einvoice migration).
    stmts: list[str] = []
    buf: list[str] = []
    i, n = 0, len(sql)
    while i < n:
        c = sql[i]
        if c in ("'", '"', "`"):
            q = c
            buf.append(c)
            i += 1
            while i < n:
                ch = sql[i]
                buf.append(ch)
                i += 1
                if ch == "\\" and q != "`" and i < n:
                    buf.append(sql[i])
                    i += 1
                elif ch == q:
                    if i < n and sql[i] == q:  # doubled-quote escape
                        buf.append(sql[i])
                        i += 1
                    else:
                        break
        elif sql.startswith("--", i) and (i == 0 or sql[i - 1] in " \t\n\r"):
            while i < n and sql[i] != "\n":
                i += 1
        elif sql.startswith("/*", i):
            j = sql.find("*/", i + 2)
            i = n if j == -1 else j + 2
        elif c == ";":
            if s := "".join(buf).strip():
                stmts.append(s)
            buf = []
            i += 1
        else:
            buf.append(c)
            i += 1
    if s := "".join(buf).strip():
        stmts.append(s)
    return stmts


def _self_test() -> None:
    assert split_statements("a; b;") == ["a", "b"]
    assert split_statements("-- c;\nSELECT 1;") == ["SELECT 1"]
    assert split_statements("/* x; y */ SELECT 2;") == ["SELECT 2"]
    assert split_statements("SELECT 'a;b -- c';") == ["SELECT 'a;b -- c'"]
    assert split_statements("SELECT 'it''s';") == ["SELECT 'it''s'"]
    assert split_statements("SELECT `w;x`, 'y\\';z';") == ["SELECT `w;x`, 'y\\';z'"]
    assert split_statements("a --tail\n;") == ["a"]
    print("self-test OK")


def main() -> int:
    if "--self-test" in sys.argv:
        _self_test()
        return 0
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    db_name, script = sys.argv[1], Path(sys.argv[2])
    dry = "--dry-run" in sys.argv

    env = dotenv_values("env/database.env")
    statements = split_statements(script.read_text(encoding="utf-8"))
    print(f"{script.name} -> {db_name}: {len(statements)} statement(s)")

    if dry:
        for i, s in enumerate(statements, 1):
            print(f"\n--- [{i}] ---\n{s}")
        return 0

    conn = pymysql.connect(
        host=env["DATABASE_HOST"],
        port=int(env["DATABASE_PORT"]),
        user=env["DATABASE_USER"],
        password=env["DATABASE_PASSWORD"],
        database=db_name,
        cursorclass=pymysql.cursors.DictCursor,
        autocommit=False,
    )
    try:
        with conn.cursor() as cur:
            for i, stmt in enumerate(statements, 1):
                cur.execute(stmt)
                print(f"  [{i}] ok, {cur.rowcount} row(s) — {stmt.splitlines()[0][:70]}")
        conn.commit()
        print("COMMITTED")
    except Exception:
        conn.rollback()
        print("ROLLED BACK")
        raise
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
