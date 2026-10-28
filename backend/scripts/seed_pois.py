#!/usr/bin/env python
"""POI 种子脚本（BR-08）：把 `data/pois_nau.csv` 灌进 `pois` 表。

## 为什么要有这个脚本

发单的起终点**必须**来自 `pois` 字典（用户不能手填坐标）。没有种子数据，
前端连一个可选项都渲染不出来，整条演示链路在发单页就断了。
它不属于任何业务模块，所以放在 `scripts/` 而不是 `app/` —— 生产环境不应该
在 import 期就跑种子。

## 幂等性

`INSERT ... ON CONFLICT (campus_id, name) DO NOTHING`：重复执行不报错、
不覆盖运营后来手改过的 `sort_hint` / `lat` / `lng`。
冲突目标用的是 0001 基线里的唯一索引 `uq_pois_campus_id_name`，
不依赖任何应用层的"先查再插"（那在并发下必然漏）。

## 跑法

```bash
export DATABASE_URL="postgresql+psycopg://campusrun:campusrun@127.0.0.1:5433/campusrun"
python scripts/seed_pois.py                 # 用默认 CSV
python scripts/seed_pois.py --csv other.csv --dry-run
```

## 坐标口径

CSV 里的 `lat` / `lng` 是 **GCJ02（高德系）**，与 `pois` 表注释一致。
若来源是 WGS84（GPS 原始坐标），必须先转换再入库 —— 直接把 WGS84 塞进
GCJ02 的列里，地图上会整体偏移 500 米左右，而这种偏移**不会报错**，
只会让"驿站"显示在隔壁小区里。
"""

from __future__ import annotations

import argparse
import csv
import os
import sys
from pathlib import Path
from typing import Any, Final

import sqlalchemy as sa

#: `backend/` 根目录（脚本可从任意 cwd 调用）
BACKEND_ROOT: Final[Path] = Path(__file__).resolve().parents[1]

#: 默认 CSV
DEFAULT_CSV: Final[Path] = BACKEND_ROOT / "data" / "pois_nau.csv"

#: POI 类型白名单（与 `app/models/poi.py::POI_TYPES` 的数量一致）。
POI_TYPES: Final[tuple[str, ...]] = (
    "STATION",
    "DORM",
    "CANTEEN",
    "BUILDING",
    "ADMIN",
    "PRINT",
    "SHOP",
)

#: 必填列（少一列就 fail-fast，而不是插一半）
REQUIRED_COLUMNS: Final[tuple[str, ...]] = ("campus_id", "name", "type", "lat", "lng")

_INSERT_SQL: Final[str] = """
INSERT INTO pois (campus_id, name, type, lat, lng, sort_hint, is_active)
VALUES (:campus_id, :name, :type, :lat, :lng, :sort_hint, true)
ON CONFLICT (campus_id, name) DO NOTHING
"""

_COUNT_SQL: Final[str] = "SELECT count(*) FROM pois WHERE campus_id = :campus_id"


def load_rows(csv_path: Path = DEFAULT_CSV) -> list[dict[str, Any]]:
    """读 CSV 并做**入库前**校验，返回可执行参数列表。

    Raises:
        SystemExit: 文件不存在 / 缺列 / 类型非法 / 坐标非法。
            用 `SystemExit` 而不是自定义异常：这是运维脚本，fail-fast +
            可读错误比可捕获的异常类型更有用（没人会 `except` 它）。
    """
    if not csv_path.is_file():
        raise SystemExit(f"[seed_pois] CSV 不存在：{csv_path}")

    with csv_path.open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        header = reader.fieldnames or []
        missing = [col for col in REQUIRED_COLUMNS if col not in header]
        if missing:
            raise SystemExit(f"[seed_pois] CSV 缺少列：{missing}（实际列：{header}）")

        rows: list[dict[str, Any]] = []
        for lineno, raw in enumerate(reader, start=2):
            row = {key: (value or "").strip() for key, value in raw.items() if key}
            if not row.get("name"):
                continue                                  # 空行/尾行
            poi_type = row["type"].upper()
            if poi_type not in POI_TYPES:
                raise SystemExit(
                    f"[seed_pois] 第 {lineno} 行 type='{poi_type}' 非法，"
                    f"只能是 {list(POI_TYPES)}"
                )
            try:
                lat = float(row["lat"])
                lng = float(row["lng"])
            except ValueError as exc:
                raise SystemExit(f"[seed_pois] 第 {lineno} 行坐标不是数字：{exc}") from exc
            if not (-90 <= lat <= 90) or not (-180 <= lng <= 180):
                raise SystemExit(f"[seed_pois] 第 {lineno} 行坐标越界：lat={lat} lng={lng}")
            rows.append(
                {
                    "campus_id": int(row.get("campus_id") or 1),
                    "name": row["name"],
                    "type": poi_type,
                    "lat": round(lat, 6),
                    "lng": round(lng, 6),
                    "sort_hint": int(row.get("sort_hint") or 0),
                }
            )
    if not rows:
        raise SystemExit(f"[seed_pois] CSV 里没有任何有效行：{csv_path}")
    return rows


def seed_pois(connection: sa.engine.Connection, rows: list[dict[str, Any]]) -> int:
    """把 `rows` 灌进 `pois`，返回**实际插入**的行数（冲突的行不计）。

    不 commit：调用方决定事务边界（`with engine.begin() as conn:`）。
    """
    inserted = 0
    for row in rows:
        result = connection.execute(sa.text(_INSERT_SQL), row)
        inserted += int(result.rowcount or 0)
    return inserted


def count_pois(connection: sa.engine.Connection, campus_id: int = 1) -> int:
    """某个校区的 POI 总数（校验用）。"""
    return int(connection.execute(sa.text(_COUNT_SQL), {"campus_id": campus_id}).scalar_one())


def main(argv: list[str] | None = None) -> int:
    """命令行入口。返回进程退出码（0 成功）。"""
    parser = argparse.ArgumentParser(description="灌入 POI 种子数据（幂等）")
    parser.add_argument("--csv", type=Path, default=DEFAULT_CSV, help="POI CSV 路径")
    parser.add_argument(
        "--database-url",
        default=os.environ.get("DATABASE_URL", ""),
        help="目标库（默认读 DATABASE_URL 环境变量）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只校验 CSV 不入库")
    args = parser.parse_args(argv)

    rows = load_rows(args.csv)
    print(f"[seed_pois] CSV 校验通过：{len(rows)} 行（{args.csv}）")
    for row in rows:
        print(f"  - {row['type']:<8} {row['name']}  ({row['lat']}, {row['lng']})")
    if args.dry_run:
        print("[seed_pois] --dry-run：未写库")
        return 0

    if not args.database_url:
        print("[seed_pois] 缺少 --database-url 或 DATABASE_URL 环境变量", file=sys.stderr)
        return 2

    engine = sa.create_engine(args.database_url, future=True)
    try:
        with engine.begin() as connection:
            inserted = seed_pois(connection, rows)
            total = count_pois(connection, rows[0]["campus_id"])
    except Exception as exc:  # noqa: BLE001 - 运维脚本：打出原因并以非 0 退出
        print(f"[seed_pois] 写库失败：{exc}", file=sys.stderr)
        return 1
    finally:
        engine.dispose()

    print(f"[seed_pois] 完成：新增 {inserted} 行，跳过 {len(rows) - inserted} 行（已存在）")
    print(f"[seed_pois] 当前 campus_id={rows[0]['campus_id']} 共 {total} 个 POI")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
