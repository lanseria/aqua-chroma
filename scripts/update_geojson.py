# scripts/update_geojson.py
#
# 从阿里 DataV GeoAtlas 下载省级行政区划边界（_full 为其下一级子区划，
# 例如 330000_full.json = 浙江省下辖 11 个地级市），合并为监测海域用的
# 高精度海岸线 GeoJSON，供海洋蒙版与陆地描边使用。
#
# 用法:
#   python scripts/update_geojson.py                # 下载默认省份(浙江+上海)
#   python scripts/update_geojson.py 330000 320000  # 指定省份 adcode
#
# adcode 对照: 浙江 330000 / 上海 310000 / 江苏 320000 ...

import json
import sys
import urllib.request
from pathlib import Path

BASE_URL = "https://geo.datav.aliyun.com/areas_v3/bound/{adcode}_full.json"
DEFAULT_ADCODES = ("330000", "310000")  # 监测窗口(杭州湾/舟山海域)覆盖的省份
OUTPUT_PATH = Path("geojson/monitor_area.geojson")


def fetch_province(adcode: str) -> dict:
    url = BASE_URL.format(adcode=adcode)
    print(f"下载 {url} ...")
    req = urllib.request.Request(url, headers={"User-Agent": "aqua-chroma/geo-updater"})
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    features = data.get("features")
    if not features:
        raise ValueError(f"{url} 返回内容不含 features，请确认 adcode 是否正确")
    return data


def main() -> None:
    adcodes = sys.argv[1:] or list(DEFAULT_ADCODES)
    merged = {"type": "FeatureCollection", "features": []}
    for adcode in adcodes:
        merged["features"].extend(fetch_province(adcode)["features"])

    OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(OUTPUT_PATH, "w", encoding="utf-8") as f:
        json.dump(merged, f, ensure_ascii=False)
    total_pts = sum(
        len(ring)
        for feat in merged["features"]
        for poly in (feat["geometry"]["coordinates"] if feat["geometry"]["type"] == "MultiPolygon"
                     else [feat["geometry"]["coordinates"]])
        for ring in poly
    )
    print(f"已合并 {len(adcodes)} 个省份、{len(merged['features'])} 个子区划、"
          f"共 {total_pts} 个边界顶点 -> {OUTPUT_PATH}")


if __name__ == "__main__":
    main()
