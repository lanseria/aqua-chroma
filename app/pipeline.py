# app/pipeline.py

import shutil
from pathlib import Path
from typing import Any, Dict, Optional

import cv2
import numpy as np
from PIL import Image

from . import config, geo_utils, processor, super_resolution

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

    # 清空旧产物，避免上次中途失败留下的残缺/过时文件与新结果混杂
    for old_file in output_dir_path.iterdir():
        if old_file.is_file() or old_file.is_symlink():
            old_file.unlink()
        else:
            shutil.rmtree(old_file)

    analysis_result = {}
    try:
        # --- 步骤 1: 输入图高清化 (预处理) ---
        # 优先使用 Real-ESRGAN x4 超分；失败自动降级为 bicubic（见 super_resolution.enhance）。
        scale_factor = config.ESRGAN_SCALE
        print(f"将图像高清化 {scale_factor} 倍 (Real-ESRGAN)...")
        image_array = np.array(image)
        upscaled_rgb = super_resolution.enhance(image_array)
        image_to_process = Image.fromarray(upscaled_rgb)
        # 保存高清化前的干净原图（无标注），与超分结果 01b 形成前后对比
        input_image_path = output_dir_path / "01_input_processed.png"
        image.save(input_image_path)
        # 保存高清化中间图（后续所有分析步骤的实际输入），便于对比高清化前后效果
        superres_image_path = output_dir_path / "01b_superresolved.png"
        image_to_process.save(superres_image_path)

        # --- 步骤 2: 自动色彩均衡 ---
        # 对高清化后的图像（即后续分析的实际输入）做均衡，保持整条链路尺寸一致
        image_bgr = cv2.cvtColor(upscaled_rgb, cv2.COLOR_RGB2BGR)
        # 调用均衡函数
        balanced_bgr = _auto_balance_color(image_bgr)
        # 保存均衡后的调试图
        balanced_image_path = output_dir_path / "02_auto_balanced.png"
        cv2.imwrite(str(balanced_image_path), balanced_bgr)
        # 将均衡后的图像 (BGR) 用于后续步骤
        image_for_analysis_bgr = balanced_bgr

        # --- 步骤 3: 创建地理蒙版 ---
        # 蒙版基于超分后的尺寸创建，与均衡图 / 海洋图 / 分类图全程尺寸一致
        ocean_mask = geo_utils.create_ocean_mask(
            image_shape=image_to_process.size[::-1],
            geojson_path=config.GEOJSON_PATH,
            bounds=config.TARGET_AREA
        )

        # --- 步骤 4: 生成带地图标注的可视化图 (陆地描边 + 城市点位) ---
        # 标注叠加在高清化图上仅供可视化，颜色分析始终使用未标注的干净图像
        img_height = upscaled_rgb.shape[0]
        outline_cfg = config.LAND_OUTLINE
        annotated = geo_utils.draw_land_outline(
            image_rgb=upscaled_rgb,
            ocean_mask=ocean_mask,
            color=outline_cfg["color"],
            thickness=outline_cfg["thickness"],
            halo_color=outline_cfg["halo_color"],
            halo_thickness=outline_cfg["halo_thickness"],
        )
        font_size = max(12, round(img_height / 24))
        marker_cfg = config.CITY_MARKER
        annotated = geo_utils.draw_city_markers(
            image_rgb=annotated,
            bounds=config.TARGET_AREA,
            cities=config.CITY_POINTS,
            marker_color=marker_cfg["marker_color"],
            marker_radius=max(2, round(img_height / 110)) if marker_cfg["radius"] > 0 else 0,
            label_color=marker_cfg["label_color"],
            label_stroke=marker_cfg["label_stroke"],
            font=geo_utils.load_cjk_font(font_size, config.FONT_CANDIDATES),
        )
        annotated_image_path = output_dir_path / "01_input_annotated.png"
        Image.fromarray(annotated).save(annotated_image_path)

        # apply_mask 期望 PIL Image, 所以我们先转换一下
        image_for_analysis_pil = Image.fromarray(cv2.cvtColor(image_for_analysis_bgr, cv2.COLOR_BGR2RGB))
        ocean_only_image_array = geo_utils.apply_mask(image_for_analysis_pil, ocean_mask)
        masked_image_path = output_dir_path / "03_ocean_only.png"
        Image.fromarray(ocean_only_image_array).save(masked_image_path)
        
        # --- 步骤 5: 核心颜色分析 (使用均衡且蒙版后的图像) ---
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