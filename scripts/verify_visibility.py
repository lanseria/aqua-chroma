# scripts/verify_visibility.py
"""
快速验证能见度估算：对 test_images 三类样本（clear_sky/cloudy/heavy_fog）
直接调用 processor.estimate_visibility，检查数值分布是否合理。
不依赖数据库与下载器。

用法：uv run python scripts/verify_visibility.py
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
from PIL import Image

from app import geo_utils, processor


def main():
    test_dir = "test_images"
    files = sorted(f for f in os.listdir(test_dir) if f.endswith(".png"))

    print(f"{'文件':<22} {'云量(模拟)':>8} {'能见度km':>9} {'等级':<10} {'雾得分':>7} {'暗通道':>7} {'对比度':>7} {'边缘':>6}")
    print("-" * 90)

    for name in files:
        image = np.array(Image.open(os.path.join(test_dir, name)).convert("RGB"))
        # 测试图为裁剪样本，直接用全图蒙版（全 255）模拟海洋区域
        mask = np.full(image.shape[:2], 255, dtype=np.uint8)

        # 先估云量：用 processor 的 HSV 云判据快速统计
        import cv2
        hsv = cv2.cvtColor(image, cv2.COLOR_RGB2HSV)
        ranges = processor.config.COLOR_CLASSIFICATION_HSV_RANGES
        cloud_mask = cv2.inRange(hsv, np.array(ranges["CLOUD"]["lower"]), np.array(ranges["CLOUD"]["upper"]))
        cloud_ratio = np.count_nonzero(cloud_mask) / (mask.shape[0] * mask.shape[1])

        result = processor.estimate_visibility(image, mask, cloud_mask=cloud_mask, output_dir=None)
        print(f"{name:<22} {cloud_ratio:>7.2%} "
              f"{str(result['visibilityKm']):>9} {result['visibilityLevel']:<10} "
              f"{str(result['hazeScore']):>7} {str(result['darkChannelBrightness']):>7} "
              f"{str(result['contrastScore']):>7} {str(result['edgeScore']):>6}")


if __name__ == "__main__":
    main()
