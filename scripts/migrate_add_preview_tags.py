# scripts/migrate_add_preview_tags.py
"""
一次性 Schema 迁移：新增 preview_tags 表，用于给分析结果时间点打「类型标签」。

背景
----
颜色阈值实时调优页只使用真实分析结果（data/output/<timestamp>/03_ocean_only.png）
作为预览源；用户可给任意历史时间点打多个 tag（如 晴朗天空/多云/薄雾），
把真实分析结果当作"分类型测试样本"，并按 tag 筛选预览源。tag 属于用户数据，
持久化在数据库，不写死在任何配置文件里。

表结构
------
    id          SERIAL / INTEGER 主键自增
    timestamp   INTEGER 非空（分析结果时间戳，秒），建索引
    tag         VARCHAR(32) 非空（单个 tag，如 多云）
    created_at  TIMESTAMPTZ 非空（写入时间）
    UNIQUE(timestamp, tag)  —— 同一时间点的同一 tag 不会重复插入

幂等：表已存在时跳过（create_all 本身也幂等），可重复执行。
应用启动时 init_db() 也会自动建表；本脚本用于已有部署环境的显式迁移。

用法（在项目根目录）：
    uv run python scripts/migrate_add_preview_tags.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import text

from app import models  # noqa: F401  确保模型已注册到 Base.metadata
from app.database import Base, engine


TABLE_NAME = "preview_tags"


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

    Base.metadata.create_all(bind=engine, tables=[models.PreviewTag.__table__])
    print(
        f"已创建表 {TABLE_NAME} "
        "(id SERIAL PRIMARY KEY, timestamp INTEGER, tag VARCHAR(32), "
        "created_at TIMESTAMPTZ, UNIQUE(timestamp, tag))"
    )
    print("可在 /tools/color_thresholds 页面给分析结果时间点打 tag（如 晴朗天空/多云/薄雾）")


if __name__ == "__main__":
    main()