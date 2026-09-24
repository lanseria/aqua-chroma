# app/schemas.py
from pydantic import BaseModel
from typing import Optional

# 模型(1): 用于向数据库写入数据，只包含数据库字段
class AnalysisResultCreate(BaseModel):
    timestamp: int
    status: str
    metric_version: int = 1
    sea_blueness: Optional[float] = None
    cloud_coverage: Optional[float] = None
    blueness_index: Optional[float] = None
    blue_pixels: Optional[int] = None
    yellow_pixels: Optional[int] = None
    cloud_pixels: Optional[int] = None
    total_ocean_pixels: Optional[int] = None

# 模型(2): 用于从数据库读取数据，精确匹配数据库表结构
class AnalysisResultFromDB(AnalysisResultCreate):
    id: int

    class Config:
        from_attributes = True

# 模型(3): API最终返回给客户端的完整结构，包含动态字段
class AnalysisResultResponse(AnalysisResultFromDB):
    output_directory: str
