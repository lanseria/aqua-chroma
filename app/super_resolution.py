# app/super_resolution.py
"""
Real-ESRGAN 图像超分（高清化）模块。

设计说明:
- RRDBNet 网络结构按 Real-ESRGAN 官方实现(realesrgan.rrdbnet / basicsr.archs.rrdbnet_arch)
  等价移植，仅保留推理所需的前向路径。不安装 realesrgan/basicsr 包:
  二者是训练/评估全家桶，依赖 tb-nightly 等已无法解析的死包。
- 权重使用官方发布的 RealESRGAN_x4plus.pth(见 config.ESRGAN_MODEL_PATH)。
- 模型在首次调用时懒加载并常驻内存，避免影响服务启动速度。
- 推理对大图做 Tiled 切块处理，限制单次前向的内存占用。
- 任何失败(权重缺失/加载失败/推理异常)统一降级为 bicubic 插值，保证主流程不中断。
"""
import logging
import threading
from typing import Optional

import numpy as np
import torch
from torch import nn
from torch.nn import functional as F

from . import config

logger = logging.getLogger(__name__)

# =================================================================
#  RRDBNet 网络定义 (与 Real-ESRGAN 官方推理结构等价)
# =================================================================

class ResidualDenseBlock(nn.Module):
    def __init__(self, nf: int = 64, gc: int = 32):
        super().__init__()
        self.conv1 = nn.Conv2d(nf, gc, 3, 1, 1)
        self.conv2 = nn.Conv2d(nf + gc, gc, 3, 1, 1)
        self.conv3 = nn.Conv2d(nf + 2 * gc, gc, 3, 1, 1)
        self.conv4 = nn.Conv2d(nf + 3 * gc, gc, 3, 1, 1)
        self.conv5 = nn.Conv2d(nf + 4 * gc, nf, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        x1 = self.lrelu(self.conv1(x))
        x2 = self.lrelu(self.conv2(torch.cat((x, x1), 1)))
        x3 = self.lrelu(self.conv3(torch.cat((x, x1, x2), 1)))
        x4 = self.lrelu(self.conv4(torch.cat((x, x1, x2, x3), 1)))
        x5 = self.conv5(torch.cat((x, x1, x2, x3, x4), 1))
        return x5 * 0.2 + x


class RRDB(nn.Module):
    """Residual in Residual Dense Block"""

    def __init__(self, nf: int, gc: int = 32):
        super().__init__()
        self.rdb1 = ResidualDenseBlock(nf, gc)
        self.rdb2 = ResidualDenseBlock(nf, gc)
        self.rdb3 = ResidualDenseBlock(nf, gc)

    def forward(self, x):
        out = self.rdb1(x)
        out = self.rdb2(out)
        out = self.rdb3(out)
        return out * 0.2 + x


class RRDBNet(nn.Module):
    """Real-ESRGAN x4 的生成器网络 (num_block=23, x4 上采样)"""

    def __init__(self, nf: int = 64, gc: int = 32):
        super().__init__()
        self.conv_first = nn.Conv2d(3, nf, 3, 1, 1)
        self.body = nn.Sequential(*[RRDB(nf, gc) for _ in range(23)])
        self.conv_body = nn.Conv2d(nf, nf, 3, 1, 1)
        # x4 上采样 = 两次 x2 (pixel shuffle)。卷积层名 conv_up1/2 与官方 checkpoint 对齐
        self.conv_up1 = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_up2 = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_hr = nn.Conv2d(nf, nf, 3, 1, 1)
        self.conv_last = nn.Conv2d(nf, 3, 3, 1, 1)
        self.lrelu = nn.LeakyReLU(negative_slope=0.2, inplace=True)

    def forward(self, x):
        feat = self.conv_first(x)
        body_feat = self.conv_body(self.body(feat))
        feat = feat + body_feat
        feat = self.lrelu(self.conv_up1(F.interpolate(feat, scale_factor=2, mode="nearest")))
        feat = self.lrelu(self.conv_up2(F.interpolate(feat, scale_factor=2, mode="nearest")))
        return self.conv_last(self.lrelu(self.conv_hr(feat)))


# =================================================================
#  推理封装
# =================================================================

_model: Optional[RRDBNet] = None
_model_lock = threading.Lock()


def _get_model() -> RRDBNet:
    """懒加载模型权重，线程安全；失败时抛出异常由调用方降级。"""
    global _model
    if _model is None:
        with _model_lock:
            if _model is None:
                weights_path = config.ESRGAN_MODEL_PATH
                if not weights_path.is_file():
                    raise FileNotFoundError(f"ESRGAN 权重不存在: {weights_path}")
                net = RRDBNet(nf=config.ESRGAN_NUM_FEAT, gc=config.ESRGAN_NUM_GROW_CH)
                state = torch.load(weights_path, map_location="cpu", weights_only=True)
                # 官方 checkpoint 外层包了一层 {'params_ema': ...}（或 params）
                if "params_ema" in state:
                    state = state["params_ema"]
                elif "params" in state:
                    state = state["params"]
                net.load_state_dict(state, strict=True)
                net.eval()
                torch.set_num_threads(config.ESRGAN_TORCH_THREADS)
                _model = net
                print(f"[ESRGAN] Model loaded from {weights_path}")
    return _model


def _forward_tiled(model: RRDBNet, img_tensor: torch.Tensor, tile: int, pad: int = 8) -> torch.Tensor:
    """
    分块前向，避免大图一次性推理内存爆炸。
    tile: 每块边长; pad: 相邻块间的重叠(缓解拼接缝)。
    """
    _, _, h, w = img_tensor.shape
    if h <= tile and w <= tile:
        return model(img_tensor)

    scale = config.ESRGAN_SCALE
    out = torch.zeros(1, 3, h * scale, w * scale)
    for y in range(0, h, tile):
        for x in range(0, w, tile):
            y0, x0 = max(y - pad, 0), max(x - pad, 0)
            y1, x1 = min(y + tile + pad, h), min(x + tile + pad, w)
            patch = model(img_tensor[:, :, y0:y1, x0:x1])
            # 裁掉 pad 对应的输出边缘，只保留中心有效区
            py0, px0 = (y - y0) * scale, (x - x0) * scale
            out[:, :, y * scale:(y + min(tile, h - y)) * scale,
                x * scale:(x + min(tile, w - x)) * scale] = \
                patch[:, :, py0:py0 + min(tile, h - y) * scale,
                      px0:px0 + min(tile, w - x) * scale]
    return out


def enhance(image_rgb: np.ndarray) -> np.ndarray:
    """
    对 RGB 图像执行 Real-ESRGAN x4 超分。

    成功: 返回 4x 尺寸的 uint8 RGB 数组;
    失败: 打印原因并降级为 bicubic 放大 (保证主流程不中断)。
    """
    if not config.ESRGAN_ENABLED:
        return _bicubic_fallback(image_rgb, "ESRGAN disabled by config")

    h, w = image_rgb.shape[:2]
    try:
        model = _get_model()
        img = torch.from_numpy(
            np.ascontiguousarray(image_rgb.transpose(2, 0, 1)).astype(np.float32) / 255.0
        ).unsqueeze(0)
        with torch.no_grad():
            out = _forward_tiled(model, img, tile=config.ESRGAN_TILE_SIZE)
        out = out.squeeze(0).clamp_(0, 1).mul(255).round().byte()
        result = out.permute(1, 2, 0).numpy()
        print(f"[ESRGAN] Enhanced {w}x{h} -> {result.shape[1]}x{result.shape[0]}")
        return result
    except Exception as e:
        return _bicubic_fallback(image_rgb, f"ESRGAN failed: {e}")


def _bicubic_fallback(image_rgb: np.ndarray, reason: str) -> np.ndarray:
    scale = config.ESRGAN_SCALE
    h, w = image_rgb.shape[:2]
    print(f"[ESRGAN] Fallback to bicubic ({reason}): {w}x{h} -> {w * scale}x{h * scale}")
    import cv2
    return cv2.resize(image_rgb, (w * scale, h * scale), interpolation=cv2.INTER_CUBIC)
