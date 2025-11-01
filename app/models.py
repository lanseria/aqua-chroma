# app/models.py
from sqlalchemy import Column, Integer, String, Float
from .database import Base

class AnalysisResult(Base):
    __tablename__ = "analysis_results"

    id = Column(Integer, primary_key=True, index=True)
    timestamp = Column(Integer, unique=True, index=True, nullable=False)
    status = Column(String, nullable=False)
    sea_blueness = Column(Float, nullable=True)
    cloud_coverage = Column(Float, nullable=True)