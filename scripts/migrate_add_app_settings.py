# scripts/migrate_add_app_settings.py
"""
一次性 Schema 迁移：新增 app_settings 表，用于持久化「运行时可编辑设置」。

背景
----
HSV 颜色阈值等调优参数不再写死在 .env / config.py 中，而是存放在
app_settings 表（key = "hsv_color_ranges"，value 为 JSON 文本）。
网页 /tools/color_thresholds 上保存的新阈值会写入该表，并立即对定时分析生效，
无需重启服务。

表结构
------
    key         VARCHAR 主键
    value       TEXT    非空，JSON 文本
    updated_at  TIMESTAMPTZ 非空，最后更新时间

幂等：表已存在时跳过（create_all 本身也幂等），可重复执行。
应用启动时 init_db() 也会自动建表；本脚本用于已有部署环境的显式迁移。

用法（在项目根目录）：
    uv run python scripts/migrate_add_app_settings.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app import models  # noqa: F401  确保模型已注册到 Base.metadata
from app.database import Base, engine


TABLE_NAME = "app_settings"


def main():
    with engine.begin() as conn:
        existing = {
            row[0]
            for row in conn.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_name = :table_name"
                ),
                {"table_name": TABLE_NAME},
            )
        }

    if TABLE_NAME in existing:
        print(f"表 {TABLE_NAME} 已存在，跳过创建")
        print("无新增变更")
        return

    Base.metadata.create_all(bind=engine, tables=[models.AppSetting.__table__])
    print(f"已创建表 {TABLE_NAME} (key VARCHAR PRIMARY KEY, value TEXT, updated_at TIMESTAMPTZ)")
    print("HSV 阈值将默认取 config.DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES，可在 /tools/color_thresholds 页面调整")


if __name__ == "__main__":
    main()