# run_test_case.py

import json
import os
from pathlib import Path

from PIL import Image

# 必须在导入app模块之前设置环境变量，因为它会影响config的加载
# 这里我们模拟在非Docker环境中运行，所以可以跳过一些设置
# 如果你的测试依赖特定的环境变量，请在这里设置
# os.environ['ACTIVE_DATA_SOURCE'] = 'ZOOM_EARTH' 

# 导入重构后的核心处理函数
# 注意：这假设你可以从项目根目录直接运行此脚本
from app.main import _process_image_pipeline


def run_all_test_cases(input_dir: str, output_dir: str):
    """
    遍历输入目录中的所有图像文件，并为每个文件运行分析流程。
    """
    input_path = Path(input_dir)
    output_path = Path(output_dir)

    if not input_path.is_dir():
        print(f"错误: 输入目录 '{input_dir}' 不存在。")
        return

    image_files = [f for f in input_path.iterdir() if f.suffix.lower() in ('.png', '.jpg', '.jpeg')]

    if not image_files:
        print(f"在 '{input_dir}' 中没有找到任何图像文件。")
        return

    print(f"--- 找到了 {len(image_files)} 个测试用例 ---")

    for image_file in image_files:
        case_name = image_file.stem
        print(f"\n>>> 正在处理测试用例: {case_name}")

        # 为每个测试用例创建一个独立的输出子目录
        case_output_path = output_path / case_name
        case_output_path.mkdir(parents=True, exist_ok=True)

        try:
            # 1. 加载本地图像
            input_image = Image.open(image_file).convert('RGB')
            
            # 2. 调用核心处理流水线
            analysis_result = _process_image_pipeline(
                image=input_image,
                output_dir_path=case_output_path
            )

            # 3. 将JSON结果保存到文件
            result_json_path = case_output_path / "results.json"
            with open(result_json_path, 'w', encoding='utf-8') as f:
                json.dump(analysis_result, f, indent=4, ensure_ascii=False)

            print(f"--- 分析完成 ---")
            print(json.dumps(analysis_result, indent=2))
            print(f"✅ 结果已保存至: {case_output_path}")

        except Exception as e:
            print(f"❌ 处理 '{case_name}' 时发生错误: {e}")


if __name__ == "__main__":
    # 定义测试图片的输入目录和结果的输出目录
    TEST_IMAGE_DIR = "test_images"
    TEST_RESULT_DIR = "test_results"

    # 确保输入目录存在
    if not os.path.exists(TEST_IMAGE_DIR):
        print(f"请创建 '{TEST_IMAGE_DIR}' 目录，并将您的原始测试图放入其中。")
    else:
        run_all_test_cases(input_dir=TEST_IMAGE_DIR, output_dir=TEST_RESULT_DIR)