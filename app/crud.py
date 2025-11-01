# app/crud.py
from sqlalchemy.orm import Session
from . import models, schemas

def get_result_by_timestamp(db: Session, timestamp: int):
    """根据时间戳查询单个分析结果。"""
    return db.query(models.AnalysisResult).filter(models.AnalysisResult.timestamp == timestamp).first()

def get_all_results(db: Session):
    """获取所有分析结果。"""
    return db.query(models.AnalysisResult).order_by(models.AnalysisResult.timestamp.desc()).all()

def get_processed_timestamps(db: Session):
    """获取所有已处理的时间戳集合。"""
    return {row[0] for row in db.query(models.AnalysisResult.timestamp).all()}

def upsert_analysis_result(db: Session, result_data: schemas.AnalysisResultCreate) -> models.AnalysisResult:
    """
    核心的 "Upsert" 函数。
    如果时间戳已存在，则更新记录；否则，创建新记录。
    """
    # 1. 尝试按时间戳查找现有记录
    db_result = get_result_by_timestamp(db, timestamp=result_data.timestamp)

    if db_result:
        # 2. 如果记录存在，则更新字段
        print(f"[CRUD] Updating existing record for timestamp: {result_data.timestamp}")
        update_data = result_data.model_dump(exclude_unset=True)
        for key, value in update_data.items():
            setattr(db_result, key, value)
    else:
        # 3. 如果记录不存在，则创建新记录
        print(f"[CRUD] Creating new record for timestamp: {result_data.timestamp}")
        db_result = models.AnalysisResult(**result_data.model_dump())
        db.add(db_result)

    # 4. 提交更改到数据库
    db.commit()
    db.refresh(db_result)
    return db_result

# 注意：我们不再需要单独的 create_analysis_result 函数，因为 upsert 已经包含了它的功能。