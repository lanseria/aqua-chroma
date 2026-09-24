# scripts/migrate_add_metric_columns.py
"""
一次性 Schema 迁移：为 analysis_results 表增加指标口径拆分所需的新列，
并将存量记录标记为 metric_version=1（旧口径：sea_blueness = 蓝水 / 全部海洋像素）。

新列（均与已有的 upsert/调度逻辑兼容，应用启动后自动使用）：
    metric_version      指标口径版本，存量记录=1，新口径记录=2
    blueness_index      综合海蓝指数 = sea_blueness * (1 - cloud_coverage)
    blue_pixels         蓝水像素数
    yellow_pixels       黄水像素数
    cloud_pixels        云像素数
    total_ocean_pixels  海洋总像素数

幂等：重复执行时先检查列是否已存在。存量数据不可回填像素计数（源图未保留计数），
仅回填 blueness_index（可由存量 sea_blueness * (1 - cloud_coverage) 精确还原）。

用法（在项目根目录）：
    uv run python scripts/migrate_add_metric_columns.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import engine


NEW_COLUMNS = {
    "metric_version": "INTEGER NOT NULL DEFAULT 1",
    "blueness_index": "DOUBLE PRECISION",
    "blue_pixels": "INTEGER",
    "yellow_pixels": "INTEGER",
    "cloud_pixels": "INTEGER",
    "total_ocean_pixels": "INTEGER",
}


def main():
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT column_name FROM information_schema.columns "
                    "WHERE table_name = 'analysis_results'"
                )
            )
        }
        print(f"现有列: {sorted(existing)}")

        added = []
        for col, ddl in NEW_COLUMNS.items():
            if col in existing:
                print(f"跳过 {col}（已存在）")
                continue
            conn.execute(text(f"ALTER TABLE analysis_results ADD COLUMN {col} {ddl}"))
            added.append(col)
            print(f"已添加列 {col} {ddl}")

        if added:
            # 存量记录全部标记为旧口径；blueness_index 可由旧口径两个字段精确还原
            conn.execute(
                text(
                    "UPDATE analysis_results SET metric_version = 1 "
                    "WHERE metric_version IS NULL"
                )
            )
            conn.execute(
                text(
                    "UPDATE analysis_results SET blueness_index = "
                    "sea_blueness * (1 - cloud_coverage) "
                    "WHERE metric_version = 1 AND sea_blueness IS NOT NULL "
                    "AND cloud_coverage IS NOT NULL"
                )
            )
            print("存量记录已标记 metric_version=1 并回填 blueness_index")
        else:
            print("无新增列，未做数据变更")


if __name__ == "__main__":
    main()
