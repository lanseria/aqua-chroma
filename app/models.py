# app/models.py
from sqlalchemy import Column, Integer, String, Float
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