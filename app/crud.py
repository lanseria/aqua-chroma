# app/crud.py
from datetime import datetime, timezone
from typing import Dict, List, Tuple

from sqlalchemy import func
from sqlalchemy.orm import Session
from . import models, schemas

# 视为"未成功处理"的状态：调度器下个周期会对这些时间戳自动重试。
# night（天文判断，确定性结果）与 completed 不在其中，避免无意义的重复计算。
RETRYABLE_STATUSES = ("download_failed", "error")

def get_result_by_timestamp(db: Session, timestamp: int):
    """根据时间戳查询单个分析结果。"""
    return db.query(models.AnalysisResult).filter(models.AnalysisResult.timestamp == timestamp).first()

def get_all_results(db: Session):
    """获取所有分析结果。"""
    return db.query(models.AnalysisResult).order_by(models.AnalysisResult.timestamp.desc()).all()

def count_analysis_results(db: Session) -> int:
    """统计分析结果总条数（启动时用于交叉校验输出目录是否与之匹配）。"""
    return int(db.query(models.AnalysisResult).count())

def get_processed_timestamps(db: Session):
    """获取所有已成功处理的时间戳集合（失败状态的时间戳不包含，以便调度器重试）。"""
    rows = db.query(models.AnalysisResult.timestamp, models.AnalysisResult.status).all()
    return {row[0] for row in rows if row[1] not in RETRYABLE_STATUSES}

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


# --- 运行时应用设置（app_settings 表）---

def get_app_setting(db: Session, key: str):
    """按 key 查询一条运行时设置，不存在时返回 None。"""
    return db.query(models.AppSetting).filter(models.AppSetting.key == key).first()


def upsert_app_setting(db: Session, key: str, value_json: str) -> models.AppSetting:
    """
    写入/更新一条运行时设置（value 为 JSON 文本）。

    存在则更新 value 与 updated_at，不存在则插入。提交失败时抛出异常，
    由调用方（settings_store）转换为对外的明确错误。
    """
    row = get_app_setting(db, key)
    now = datetime.now(timezone.utc)
    if row:
        print(f"[CRUD] Updating app setting: {key}")
        row.value = value_json
        row.updated_at = now
    else:
        print(f"[CRUD] Creating app setting: {key}")
        row = models.AppSetting(key=key, value=value_json, updated_at=now)
        db.add(row)
    db.commit()
    db.refresh(row)
    return row


def delete_app_setting(db: Session, key: str) -> bool:
    """删除一条运行时设置。返回是否确实删除了记录。"""
    row = get_app_setting(db, key)
    if row is None:
        return False
    print(f"[CRUD] Deleting app setting: {key}")
    db.delete(row)
    db.commit()
    return True


# --- 预览源类型标签（preview_tags 表）---
# 用于给 data/output/<timestamp>/ 的分析结果打 tag（如 晴朗天空/多云/薄雾），
# 把真实分析结果当作分类型测试样本，颜色阈值调优页可按 tag 筛选预览源。

def get_preview_tags(db: Session, timestamp: int) -> List[str]:
    """获取某个时间点的全部 tag（按 tag 升序）。"""
    rows = (
        db.query(models.PreviewTag.tag)
        .filter(models.PreviewTag.timestamp == timestamp)
        .order_by(models.PreviewTag.tag.asc())
        .all()
    )
    return [row[0] for row in rows]


def get_preview_tags_map(db: Session, timestamps: List[int]) -> Dict[int, List[str]]:
    """
    批量获取多个时间点的 tag（一次查询，避免 N+1）。

    返回 {timestamp: [tag, ...]}；没有 tag 的时间点也以空列表出现，方便调用方直接索引。
    """
    result: Dict[int, List[str]] = {ts: [] for ts in timestamps}
    if not timestamps:
        return result

    rows = (
        db.query(models.PreviewTag.timestamp, models.PreviewTag.tag)
        .filter(models.PreviewTag.timestamp.in_(timestamps))
        .order_by(models.PreviewTag.timestamp.asc(), models.PreviewTag.tag.asc())
        .all()
    )
    for ts, tag in rows:
        result.setdefault(ts, []).append(tag)
    return result


def list_preview_tag_counts(db: Session) -> List[Tuple[str, int]]:
    """
    全库 tag 使用统计：返回 [(tag, 打该 tag 的时间戳数), ...]。

    排序：数量降序，数量相同再按 tag 升序（中文按 Unicode 码点，结果确定）。
    """
    rows = (
        db.query(
            models.PreviewTag.tag,
            func.count(func.distinct(models.PreviewTag.timestamp)),
        )
        .group_by(models.PreviewTag.tag)
        .all()
    )
    counts = [(tag, int(count)) for tag, count in rows]
    counts.sort(key=lambda item: (-item[1], item[0]))
    return counts


def set_preview_tags(db: Session, timestamp: int, tags: List[str]) -> List[str]:
    """
    替换式保存某个时间点的 tag：先删除该时间点全部行，再插入新 tag（单事务提交）。

    入参 tags 应已由 API 层校验/去重；这里再做一次保序去重，
    确保同一 (timestamp, tag) 不会重复插入（表上也有唯一约束兜底）。
    返回保存后该时间点的 tag 列表（按 tag 升序）。
    """
    normalized: List[str] = []
    seen = set()
    for tag in tags:
        if tag in seen:
            continue
        seen.add(tag)
        normalized.append(tag)

    db.query(models.PreviewTag).filter(
        models.PreviewTag.timestamp == timestamp
    ).delete(synchronize_session=False)

    now = datetime.now(timezone.utc)
    for tag in normalized:
        db.add(models.PreviewTag(timestamp=timestamp, tag=tag, created_at=now))
    db.commit()
    print(f"[CRUD] Set preview tags for {timestamp}: {normalized}")
    return get_preview_tags(db, timestamp)