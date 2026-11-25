# tests/catalog_fixture.py
"""Восстановление утверждённого data-only dump исключительно в отдельную test schema."""

import re
import subprocess
from pathlib import Path


def restore_categories(engine) -> dict:
    """Передаёт COPY в текущий search_path, не исполняя команды/DDL из pg_dump."""
    path = Path("data/fixtures/categories_fixture.dump")
    if not path.exists():
        raise FileNotFoundError("Нет утверждённой categories_fixture.dump")
    result = subprocess.run(
        ["pg_restore", "--data-only", "--no-owner", "--no-privileges", "--file=-", str(path)],
        capture_output=True,
        check=True,
    )
    lines = result.stdout.decode("utf-8").splitlines(keepends=True)
    raw = engine.raw_connection()
    try:
        with raw.cursor() as cursor:
            cursor.execute("SELECT current_schema()")
            schema = cursor.fetchone()[0]
            if not schema.startswith("taxonomy_test_"):
                raise ValueError("Restore разрешён только в изолированную taxonomy_test schema")
            index = 0
            restored = {}
            while index < len(lines):
                match = re.fullmatch(
                    r"COPY public\.(taxonomy_snapshots|categories) "
                    r"\(([a-z0-9_, ]+)\) FROM stdin;\n",
                    lines[index],
                )
                if not match:
                    index += 1
                    continue
                table, columns = match.groups()
                index += 1
                count = 0
                with cursor.copy(f'COPY "{schema}".{table} ({columns}) FROM STDIN') as copy:
                    while lines[index].rstrip("\n") != "\\.":
                        copy.write(lines[index])
                        count += 1
                        index += 1
                restored[table] = count
            raw.commit()
            return restored
    except Exception:
        raw.rollback()
        raise
    finally:
        raw.close()
