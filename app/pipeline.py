# app/pipeline.py

from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np
from PIL import Image

from . import config, geo_utils, processor

def _auto_balance_color(image_bgr: np.ndarray) -> np.ndarray:
    """
    使用 CLAHE 算法在 LAB 颜色空间上自动均衡图像的亮度和对比度。
    """
    print("--- [Pipeline] Performing auto color balance (CLAHE)...")
    
    # 1. 将图像从 BGR 转换到 LAB 颜色空间
    lab_image = cv2.cvtColor(image_bgr, cv2.COLOR_BGR2LAB)
    
    # 2. 分离 L, A, B 通道
    l_channel, a_channel, b_channel = cv2.split(lab_image)
    
    # 3. 创建 CLAHE 对象 (clipLimit 控制对比度限制，tileGridSize 控制局部区域大小)
    clahe = cv2.createCLAHE(clipLimit=2.0, tileGridSize=(8, 8))
    
    # 4. 仅对 L (亮度) 通道应用 CLAHE
    enhanced_l_channel = clahe.apply(l_channel)
    
    # 5. 合并增强后的 L 通道和原始的 A, B 通道
    merged_lab_image = cv2.merge([enhanced_l_channel, a_channel, b_channel])
    
    # 6. 将图像从 LAB 转换回 BGR 颜色空间
    balanced_bgr_image = cv2.cvtColor(merged_lab_image, cv2.COLOR_LAB2BGR)
    
    print("--- [Pipeline] Auto color balance complete.")
    return balanced_bgr_image

def process_image_pipeline(image: Image.Image, output_dir_path: Path, hsv_ranges_override: Optional[Dict] = None) -> Dict[str, Any]:
    """
    接收一个PIL图像，执行完整的分析流程，并保存所有中间调试图。
    这是被主任务和调试工具共享的核心可重用逻辑。

    Args:
        image: 输入的 PIL.Image.Image 对象。
        output_dir_path: 用于保存所有输出文件的 pathlib.Path 对象。
        hsv_ranges_override: 可选的HSV参数字典，用于覆盖默认配置。

    Returns:
        一个包含详细分析结果的字典。
    """
    # 确保输出目录存在
    output_dir_path.mkdir(parents=True, exist_ok=True)
    
    analysis_result = {}
    try:
        # --- 步骤 1: 根据配置放大图像 (预处理) ---
        scale_factor = config.PRE_ANALYSIS_SCALE_FACTOR
        if scale_factor > 1.0:
            print(f"将图像放大 {scale_factor} 倍...")
            image_bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
            new_width = int(image_bgr.shape[1] * scale_factor)
            new_height = int(image_bgr.shape[0] * scale_factor)
            upscaled_bgr = cv2.resize(image_bgr, (new_width, new_height), interpolation=cv2.INTER_CUBIC)
            image_to_process = Image.fromarray(cv2.cvtColor(upscaled_bgr, cv2.COLOR_BGR2RGB))
        else:
            image_to_process = image
        
        # --- 步骤 2: 保存预处理后的输入图 ---
        input_image_path = output_dir_path / "01_input_processed.png"
        image_to_process.save(input_image_path)

        # --- 新增步骤 2.5: 自动色彩均衡 ---
        # 将 PIL Image 转换为 OpenCV BGR 格式
        image_bgr = cv2.cvtColor(np.array(image_to_process), cv2.COLOR_RGB2BGR)
        # 调用均衡函数
        balanced_bgr = _auto_balance_color(image_bgr)
        # 保存均衡后的调试图
        balanced_image_path = output_dir_path / "02_auto_balanced.png"
        cv2.imwrite(str(balanced_image_path), balanced_bgr)
        # 将均衡后的图像 (BGR) 用于后续步骤
        image_for_analysis_bgr = balanced_bgr

        # --- 步骤 3: 创建并应用地理蒙版 (使用均衡后的图像) ---
        # 注意：现在 image_for_analysis_bgr 是我们的数据源
        ocean_mask = geo_utils.create_ocean_mask(
            image_shape=image_for_analysis_bgr.shape,
            geojson_path=config.GEOJSON_PATH,
            bounds=config.TARGET_AREA
        )
        
        # apply_mask 期望 PIL Image, 所以我们先转换一下
        image_for_analysis_pil = Image.fromarray(cv2.cvtColor(image_for_analysis_bgr, cv2.COLOR_BGR2RGB))
        ocean_only_image_array = geo_utils.apply_mask(image_for_analysis_pil, ocean_mask)
        masked_image_path = output_dir_path / "03_ocean_only.png"
        Image.fromarray(ocean_only_image_array).save(masked_image_path)
        
        # --- 步骤 4: 核心颜色分析 (使用均衡且蒙版后的图像) ---
        # analyze_ocean_color 期望 RGB array
        analysis_result = processor.analyze_ocean_color(
            image_array=ocean_only_image_array,
            ocean_mask=ocean_mask,
            output_dir=str(output_dir_path),
            hsv_ranges_override=hsv_ranges_override
        )

    except Exception as e:
        print(f"An unexpected error occurred during image processing pipeline: {e}")
        analysis_result = {"status": "error"}
        
    return analysis_result