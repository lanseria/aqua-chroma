# app/models.py
from sqlalchemy import Column, DateTime, Integer, String, Float, Text, UniqueConstraint
from .database import Base

class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(Integer, unique=True, index=True, nullable=False)
    status = Column(String, nullable=False)
    # metric_version=1（旧口径）: sea_blueness = 蓝水像素 / 全部海洋像素（含云）
    # metric_version=2（新口径）: sea_blueness = 蓝水像素 / 可见水体像素（蓝+黄），与云量无关；
    #                              blueness_index = sea_blueness * (1 - cloud_coverage) 承接旧口径的综合语义
    metric_version = Column(Integer, nullable=False, default=1)
    sea_blueness = Column(Float, nullable=True)
    cloud_coverage = Column(Float, nullable=True)
    blueness_index = Column(Float, nullable=True)
    # 能见度估算（公里）：在无云水面上按暗通道亮度/局部RMS对比度/边缘密度反演；
    # 无云占比不足时按比例混入含云顶的全场景测量，云越厚能见度自然越低（无硬截断）。
    # NULL 表示无法估算（如无海洋像素）。历史数据中的 0 值为旧版"云层遮蔽"硬截断。
    visibility_km = Column(Float, nullable=True)
    visibility_level = Column(String, nullable=True)
    # 能见度中间代理量，便于事后审计与调参
    haze_score = Column(Float, nullable=True)
    blue_pixels = Column(Integer, nullable=True)
    yellow_pixels = Column(Integer, nullable=True)
    cloud_pixels = Column(Integer, nullable=True)
    total_ocean_pixels = Column(Integer, nullable=True)


class AppSetting(Base):
    """
    运行时应用设置（键值对，value 为 JSON 文本）。

    用于存放可在网页上在线调整、且调整后立即生效的配置（当前为 HSV 颜色阈值，
    key = "hsv_color_ranges"）。配置不再写死在 .env / config.py 中：
    数据库无记录时回退 config.DEFAULT_COLOR_CLASSIFICATION_HSV_RANGES 兜底。
    """
    __tablename__ = "app_settings"

    key = Column(String, primary_key=True)
    value = Column(Text, nullable=False)          # JSON 文本
    updated_at = Column(DateTime(timezone=True), nullable=False)


class PreviewTag(Base):
    """
    分析结果时间点的自定义类型标签（tag）。

    把真实分析结果当作"分类型测试样本"：例如给某时间点打上 晴朗天空/多云/薄雾，
    颜色阈值调优页即可按 tag 筛选预览源。tag 是用户数据（持久化在数据库），
    不是写死的配置；PRESETS 只是页面上的录入候选建议。

    同一个时间点可以打多个 tag；同一 (timestamp, tag) 由唯一约束保证不重复。
    """
    __tablename__ = "preview_tags"

    id = Column(Integer, primary_key=True, autoincrement=True)
    timestamp = Column(Integer, nullable=False, index=True)   # 分析结果时间戳（秒）
    tag = Column(String(32), nullable=False)
    created_at = Column(DateTime(timezone=True), nullable=False)

    __table_args__ = (
        UniqueConstraint("timestamp", "tag", name="uq_preview_tags_timestamp_tag"),
    )