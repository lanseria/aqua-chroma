# scripts/migrate_data.py
"""
一次性数据迁移工具：把源库（Supabase）中的 analysis_results 全量搬到目标库。

用法（在项目根目录）：
    SOURCE_DATABASE_URL="postgresql://..." TARGET_DATABASE_URL="postgresql://..." \
        uv run python scripts/migrate_data.py

可选：
    --truncate  迁移前清空目标库同名表（避免与已有数据冲突）
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.database import Base, init_db
from app.models import AnalysisResult


def build_session(url_env: str):
    url = os.getenv(url_env)
    if not url:
        print(f"错误: 缺少环境变量 {url_env}")
        sys.exit(1)
    engine = create_engine(url)
    return sessionmaker(autocommit=False, autoflush=False, bind=engine)()


def main():
    parser = argparse.ArgumentParser(description="迁移 analysis_results 数据")
    parser.add_argument("--truncate", action="store_true", help="迁移前清空目标表")
    args = parser.parse_args()

    src_session = build_session("SOURCE_DATABASE_URL")
    dst_session = build_session("TARGET_DATABASE_URL")

    try:
        # 在目标库建表（幂等，已存在则跳过）
        dst_engine = dst_session.get_bind()
        Base.metadata.create_all(bind=dst_engine)

        if args.truncate:
            deleted = dst_session.query(AnalysisResult).delete()
            dst_session.commit()
            print(f"已清空目标表 analysis_results（删除 {deleted} 条）")

        rows = src_session.query(AnalysisResult).order_by(AnalysisResult.timestamp).all()
        print(f"源库共 {len(rows)} 条记录")

        # 目标库已存在的时间戳，用源数据覆盖（与线上 upsert 语义一致）
        existing = {
            row[0]
            for row in dst_session.query(AnalysisResult.timestamp).all()
        }
        inserted = updated = 0
        for row in rows:
            data = {
                "timestamp": row.timestamp,
                "status": row.status,
                "sea_blueness": row.sea_blueness,
                "cloud_coverage": row.cloud_coverage,
            }
            if row.timestamp in existing:
                dst_session.query(AnalysisResult).filter(
                    AnalysisResult.timestamp == row.timestamp
                ).update(data, synchronize_session=False)
                updated += 1
            else:
                dst_session.add(AnalysisResult(**data))
                inserted += 1

        dst_session.commit()
        print(f"迁移完成: 新增 {inserted} 条, 覆盖更新 {updated} 条")

        # 校验两侧记录数
        src_count = src_session.query(AnalysisResult).count()
        dst_count = dst_session.query(AnalysisResult).count()
        print(f"校验: 源库 {src_count} 条 / 目标库 {dst_count} 条")
        if src_count != dst_count:
            print("警告: 两侧记录数不一致，请检查！")
            sys.exit(2)
    finally:
        src_session.close()
        dst_session.close()


if __name__ == "__main__":
    main()
