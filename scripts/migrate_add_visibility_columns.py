# scripts/migrate_add_visibility_columns.py
"""
一次性 Schema 迁移：为 analysis_results 表增加能见度估算所需的新列。

新列（均与已有的 upsert/调度逻辑兼容，应用启动后自动使用）：
    visibility_km    能见度估算值（公里）。云量 ≥ VISIBILITY_ZERO_CLOUD_THRESHOLD(默认0.5)
                     时记 0（云顶遮蔽，卫星图判断不了）；晴好天气按暗通道亮度、局部
                     RMS 对比度、边缘密度三项大气浑浊度代理量反演。NULL=无法估算。
    visibility_level 能见度等级标签（良好/中等/轻度霾雾/浓雾/云层遮蔽/无数据）。
    haze_score       大气浑浊度综合得分（0=极通透，1=浓雾），便于事后审计与调参。

幂等：重复执行时先检查列是否已存在。存量数据无法回填（源图的中间代理量未保留），
新增列保持 NULL，表示"该批记录早于能见度功能"。

用法（在项目根目录）：
    uv run python scripts/migrate_add_visibility_columns.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app.database import engine


NEW_COLUMNS = {
    "visibility_km": "DOUBLE PRECISION",
    "visibility_level": "VARCHAR",
    "haze_score": "DOUBLE PRECISION",
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

        if not added:
            print("无新增列，未做数据变更")


if __name__ == "__main__":
    main()
