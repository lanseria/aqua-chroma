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
    blue_pixels = Column(Integer, nullable=True)
    yellow_pixels = Column(Integer, nullable=True)
    cloud_pixels = Column(Integer, nullable=True)
    total_ocean_pixels = Column(Integer, nullable=True)