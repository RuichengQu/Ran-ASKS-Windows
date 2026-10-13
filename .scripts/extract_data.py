#!/usr/bin/env python3
"""Offline, source-bound raster curve and scatter digitization."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import math
import shlex
import shutil
import sys
from datetime import datetime
from pathlib import Path

import agent_task
import image_ocr

REPO = Path(__file__).resolve().parent.parent
SPEC_SCHEMA = "image-data-spec-v1"
SOURCE_SCHEMA = "image-data-source-v1"
MAX_JSON_BYTES = 1024 * 1024
MAX_MARKER_COMPONENTS = 5000
CSV_FIELDS = ["series_id", "x", "y", "sigma_x", "sigma_y", "point_kind",
              "quality_flag", "x_pixel", "y_pixel", "y_pixel_min", "y_pixel_max",
              "point_id", "x_pixel_min", "x_pixel_max"]


def require(condition, message):
    if not condition:
        raise ValueError(message)


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_json(path: Path) -> dict:
    require(path.is_file() and path.stat().st_size <= MAX_JSON_BYTES, "JSON 不存在或超过 1 MiB")
    value = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(value, dict), "JSON 顶层必须是对象")
    return value


def write_json(path: Path, value: dict):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def number(value, label):
    require(isinstance(value, (int, float)) and not isinstance(value, bool)
            and math.isfinite(value), f"{label} 必须是有限数值")
    return float(value)


def integer(value, minimum, maximum, label):
    require(isinstance(value, int) and not isinstance(value, bool)
            and minimum <= value <= maximum, f"{label} 必须是 {minimum}..{maximum} 的整数")
    return value


def integer_range(value, maximum, label):
    require(isinstance(value, list) and len(value) == 2, f"{label} 须是 [min, max]")
    lower = integer(value[0], 1, maximum, label)
    upper = integer(value[1], lower, maximum, label)
    return lower, upper


def output_path(path: Path, *, staging=False) -> Path:
    path = path.absolute()
    resolved = path.resolve()
    for candidate in (path, resolved):
        require(not any(part.lower() in {"raw", "wiki", "private", "graph.db"}
                        for part in candidate.parts), "不得写入 Raw/Wiki/private/Graph")
    require(path.is_relative_to(REPO) and resolved.is_relative_to(REPO.resolve()),
            "输出须位于当前仓库 temp/ 或研究项目 outputs/ 内")
    parts = path.relative_to(REPO).parts
    real_parts = resolved.relative_to(REPO.resolve()).parts
    def allowed(parts):
        return (len(parts) > 1 and parts[0] == "temp") or (
            not staging and len(parts) >= 4 and parts[0] == "projects"
            and "outputs" in parts[2:-1])
    require(allowed(parts) and allowed(real_parts), "暂存仅写 temp/，持久产物仅写 projects/.../outputs/ 子目录")
    require(not path.exists() and not path.is_symlink(), "输出目录已存在；新运行须使用新目录，不能覆盖")
    return resolved


def prepare(source: Path, output: Path, source_locator: str = "") -> dict:
    source = source.resolve()
    require(not any(part.casefold() == "private" for part in source.parts), "首版不支持 private 图片")
    output = output_path(output, staging=True)
    info, png = image_ocr.read_image(source)
    manifest = {"schema": SOURCE_SCHEMA, "source_path": str(source),
                "source_sha256": info["sha256"], "normalized_sha256": digest(png),
                "width": info["width"], "height": info["height"],
                "source_locator": source_locator, "locator_status": "user_supplied" if source_locator else "absent"}
    spec = {"schema": SPEC_SCHEMA, "source_sha256": info["sha256"],
            "chart_type": None, "plot_bbox": None,
            "axes": {"x": {"scale": None, "unit": "", "ticks": [], "checks": []},
                     "y": {"scale": None, "unit": "", "ticks": [], "checks": []}},
            "series": [], "column_step": 1, "max_band_height_pixels": 12,
            "calibration_review": {"status": "pending", "reviewer": ""}}
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    try:
        (output / "normalized.png").write_bytes(png)
        write_json(output / "source.json", manifest)
        write_json(output / "spec.json", spec)
        relative = output.relative_to(REPO).as_posix()
        command = f"python3 .scripts/extract_data.py {{action}} --spec {shlex.quote(relative + '/spec.json')}"
        task = agent_task.make_task(
            kind="image-data-calibration", transaction_id="image-data-" + relative,
            inputs=[{"name": "image", "path": relative + "/normalized.png"},
                    {"name": "source", "path": relative + "/source.json"},
                    {"name": "contract", "path": "operations/EXTRACT_DATA.md"}],
            outputs=[{"name": "specification", "path": relative + "/spec.json", "required": True}],
            protocol={"name": SPEC_SCHEMA, "required_fields": ["chart_type", "plot_bbox", "axes", "series", "calibration_review"],
                      "coordinate_space": "EXIF-normalized-image-pixels", "numeric_data": "computed_from_pixels_only"},
            issues=[{"code": "calibration_required", "question": "确认图型、目标 Panel、系列颜色、标记形状/尺寸、轴型、刻度及单位；不支持的图型停止，不猜数值。"}],
            commands={"check": command.format(action="check"), "commit": command.format(action="extract")},
            context={"source_sha256": info["sha256"], "size": [info["width"], info["height"]],
                     "result_directory": relative + "/result"})
        return {"status": "prepared", "source_path": str(source), "spec": relative + "/spec.json", "agent_task": task}
    except Exception:
        shutil.rmtree(output)
        raise


def calibrate(axis: dict, size: int, label: str) -> dict:
    require(isinstance(axis, dict) and axis.get("scale") in {"linear", "log"}, f"{label} 轴须明确 linear/log")
    require(isinstance(axis.get("unit", ""), str), "轴单位须是字符串")
    ticks = axis.get("ticks")
    checks = axis.get("checks", [])
    require(isinstance(ticks, list) and 2 <= len(ticks) <= 32, f"{label} 轴需要 2..32 个刻度")
    require(isinstance(checks, list) and len(checks) <= 32, "独立校验刻度格式错误")
    def transform(tick):
        require(isinstance(tick, dict), "刻度须包含 pixel/value")
        pixel = number(tick.get("pixel"), "刻度像素")
        value = number(tick.get("value"), "刻度值")
        require(0 <= pixel < size, "刻度像素超出规范化图片")
        if axis["scale"] == "log":
            require(value > 0, "对数轴刻度值必须为正")
            value = math.log10(value)
        return pixel, value
    pairs = [transform(tick) for tick in ticks]
    pixels = [pair[0] for pair in pairs]
    values = [pair[1] for pair in pairs]
    require(len(set(pixels)) == len(pixels) and len(set(values)) == len(values), "标定刻度不能重复")
    mean_pixel, mean_value = math.fsum(pixels) / len(pixels), math.fsum(values) / len(values)
    denominator = math.fsum((pixel - mean_pixel) ** 2 for pixel in pixels)
    slope = math.fsum((pixel - mean_pixel) * (value - mean_value) for pixel, value in pairs) / denominator
    offset = mean_value - slope * mean_pixel
    require(math.isfinite(slope) and slope != 0 and math.isfinite(offset), "坐标变换退化或溢出")
    tolerance = number(axis.get("max_residual_pixels", 2), "校准像素容差")
    require(0 < tolerance <= 10, "校准像素容差须在 (0, 10] 内")
    residuals = [abs((value - offset) / slope - pixel) for pixel, value in pairs]
    independent = []
    for tick in checks:
        pixel, value = transform(tick)
        require(pixel not in pixels, "独立校验刻度不能重复标定刻度")
        independent.append(abs((value - offset) / slope - pixel))
    require(max(residuals + independent) <= tolerance, f"{label} 轴刻度与声明轴型不一致，须重新校准")
    return {"scale": axis["scale"], "unit": axis.get("unit", ""), "slope": slope,
            "offset": offset, "ticks": ticks, "checks": checks,
            "residual_tolerance_pixels": tolerance,
            "max_residual_pixels": max(residuals), "check_residuals_pixels": independent}


def coordinate(axis: dict, pixel: float) -> float:
    value = axis["slope"] * pixel + axis["offset"]
    if axis["scale"] == "log":
        value = 10.0 ** value
        require(value > 0, "坐标变换下溢")
    require(math.isfinite(value), "坐标变换溢出")
    return value


def load_spec(path: Path):
    from PIL import Image
    path = path.resolve()
    spec = read_json(path)
    source = read_json(path.parent / "source.json")
    require(spec.get("schema") == SPEC_SCHEMA and source.get("schema") == SOURCE_SCHEMA, "协议版本错误")
    original = Path(source["source_path"]).resolve()
    require(not any(part.casefold() == "private" for part in original.parts), "首版不支持 private 图片")
    info, png = image_ocr.read_image(original)
    require(info["sha256"] == source.get("source_sha256") == spec.get("source_sha256"), "原图 hash 不一致")
    require(digest(png) == source.get("normalized_sha256"), "规范化图片 hash 不一致")
    preview = path.parent / "normalized.png"
    if preview.exists():
        require(preview.stat().st_size <= image_ocr.MAX_BYTES
                and digest(preview.read_bytes()) == source["normalized_sha256"], "规范化图片 hash 不一致")
    require([info["width"], info["height"]] == [source.get("width"), source.get("height")], "图片尺寸绑定错误")
    require(spec.get("chart_type") in ("cartesian_curve", "cartesian_scatter"),
            "首版只支持二维笛卡尔坐标曲线/独立散点标记，不支持热图/双轴/断轴")
    bbox = spec.get("plot_bbox")
    require(isinstance(bbox, list) and len(bbox) == 4, "plot_bbox 须是 [left, top, right, bottom]，右/下界不包含")
    for index, value in enumerate(bbox):
        integer(value, 0, info["width"] if index % 2 == 0 else info["height"], "Panel 边界")
    left, top, right, bottom = bbox
    require(left < right and top < bottom and (right - left) * (bottom - top) <= 4_000_000
            and max(right - left, bottom - top) <= 8000, "Panel 边界无效或超过 400 万像素/8000 像素边长")
    require(isinstance(spec.get("axes"), dict), "缺少 axes")
    axes = {label: calibrate(spec["axes"].get(label), info["width"] if label == "x" else info["height"], label)
            for label in ("x", "y")}
    for label, endpoints in (("x", (left, right - 1)), ("y", (top, bottom - 1))):
        for endpoint in endpoints:
            coordinate(axes[label], endpoint)
    series = spec.get("series")
    require(isinstance(series, list) and 1 <= len(series) <= 12, "需明确 1..12 个目标系列")
    identifiers = set()
    for item in series:
        require(isinstance(item, dict) and isinstance(item.get("id"), str)
                and item["id"].strip() and len(item["id"]) <= 100, "系列须有非空 id")
        require(item["id"] not in identifiers, "系列 id 重复")
        require(item["id"].isprintable() and item["id"].lstrip()[0] not in "=+-@", "系列 id 不得包含控制字符或电子表格公式前缀")
        identifiers.add(item["id"])
        rgb = item.get("rgb")
        require(isinstance(rgb, list) and len(rgb) == 3, "目标颜色须为 RGB 三元组")
        for value in rgb:
            integer(value, 0, 255, "RGB 通道")
        tolerance = number(item.get("color_tolerance", 40), "颜色容差")
        require(0 <= tolerance <= 120, "颜色距离容差须在 0..120 内")
        if spec["chart_type"] == "cartesian_scatter":
            require(item.get("marker_shape") == "circle", "散点需明确 marker_shape=circle；非圆形标记锚点尚不支持")
            integer_range(item.get("marker_area_pixels"), 10000, "散点标记面积范围")
            integer_range(item.get("marker_span_pixels"), 100, "散点标记宽高范围")
    if spec["chart_type"] == "cartesian_curve":
        integer(spec.get("column_step", 1), 1, 32, "column_step")
        integer(spec.get("max_band_height_pixels", 12), 1, 100, "max_band_height_pixels")
    review = spec.get("calibration_review")
    require(isinstance(review, dict) and review.get("status") == "confirmed"
            and isinstance(review.get("reviewer"), str) and review["reviewer"].strip(), "需明确记录校准确认者")
    image = Image.open(io.BytesIO(png)).convert("RGB")
    return spec, source, axes, image


def point_row(series_id: str, point_id: str, axes: dict, x_pixel: float, y_pixel: float, bounds: list) -> dict:
    return {"series_id": series_id, "point_id": point_id,
            "x": coordinate(axes["x"], x_pixel), "y": coordinate(axes["y"], y_pixel),
            "sigma_x": "", "sigma_y": "", "point_kind": "raster_digitized", "quality_flag": "automatic",
            "x_pixel": x_pixel, "y_pixel": y_pixel,
            "x_pixel_min": bounds[0], "y_pixel_min": bounds[1],
            "x_pixel_max": bounds[2] - 1, "y_pixel_max": bounds[3] - 1}


def reject_candidate(series_id: str, candidate_id: str, bounds: list, reasons: list) -> dict:
    return {"series_id": series_id, "candidate_id": candidate_id, "pixel_bbox": bounds,
            "quality_flag": "rejected", "reasons": reasons}


def digitize_curves(spec: dict, axes: dict, masks: list, overlap):
    import numpy as np
    left, top, right, bottom = spec["plot_bbox"]
    step = spec.get("column_step", 1)
    max_height = spec.get("max_band_height_pixels", 12)
    rows, summaries, rejected = [], [], []
    for series, mask in zip(spec["series"], masks):
        summary = {"series_id": series["id"], "sampled_columns": len(range(0, right - left, step)),
                   "points": 0, "empty_columns": 0, "ambiguous_columns": 0,
                   "coverage_definition": "accepted_columns / sampled_columns"}
        for column in range(0, right - left, step):
            positions = np.flatnonzero(mask[:, column])
            if not len(positions):
                summary["empty_columns"] += 1
                continue
            bounds = [left + column, top + int(positions[0]), left + column + 1, top + int(positions[-1]) + 1]
            point_id = f"{series['id']}:column:{left + column}"
            reasons = []
            if np.any(overlap[positions, column] > 1):
                reasons.append("color_overlap")
            if np.any(np.diff(positions) > 1):
                reasons.append("multiple_bands")
            if int(positions[-1] - positions[0] + 1) > max_height:
                reasons.append("band_too_tall")
            if reasons:
                summary["ambiguous_columns"] += 1
                rejected.append(reject_candidate(series["id"], point_id, bounds, reasons))
                continue
            x_pixel, y_pixel = left + column, top + float(np.mean(positions))
            rows.append(point_row(series["id"], point_id, axes, x_pixel, y_pixel, bounds))
            summary["points"] += 1
        summary["coverage"] = summary["points"] / summary["sampled_columns"]
        summaries.append(summary)
    return rows, summaries, rejected


def digitize_scatter(spec: dict, axes: dict, masks: list, overlap):
    import numpy as np
    from scipy import ndimage
    left, top, right, bottom = spec["plot_bbox"]
    rows, summaries, rejected = [], [], []
    for series, mask in zip(spec["series"], masks):
        labels, count = ndimage.label(mask, structure=np.ones((3, 3), dtype=np.uint8))
        count = int(count)
        require(count <= MAX_MARKER_COMPONENTS, f"散点候选超过 {MAX_MARKER_COMPONENTS}；缩小 Panel 或收紧颜色范围")
        area_counts = np.bincount(labels.ravel(), minlength=count + 1)
        overlap_counts = np.bincount(labels[overlap > 1], minlength=count + 1)
        min_area, max_area = series["marker_area_pixels"]
        min_span, max_span = series["marker_span_pixels"]
        series_rows = []
        for component_id, region in enumerate(ndimage.find_objects(labels, max_label=count), start=1):
            if region is None:
                continue
            row_slice, column_slice = region
            bounds = [left + column_slice.start, top + row_slice.start,
                      left + column_slice.stop, top + row_slice.stop]
            point_id = f"{series['id']}:marker:{component_id}"
            width, height = bounds[2] - bounds[0], bounds[3] - bounds[1]
            reasons = []
            if not min_area <= area_counts[component_id] <= max_area:
                reasons.append("marker_area_out_of_range")
            if not min_span <= width <= max_span or not min_span <= height <= max_span:
                reasons.append("marker_span_out_of_range")
            if bounds[0] == left or bounds[1] == top or bounds[2] == right or bounds[3] == bottom:
                reasons.append("boundary_touch")
            if overlap_counts[component_id]:
                reasons.append("color_overlap")
            if reasons:
                rejected.append(reject_candidate(series["id"], point_id, bounds, reasons))
                continue
            row_positions, column_positions = np.nonzero(labels[region] == component_id)
            x_pixel = bounds[0] + float(np.mean(column_positions))
            y_pixel = bounds[1] + float(np.mean(row_positions))
            series_rows.append(point_row(series["id"], point_id, axes, x_pixel, y_pixel, bounds))
        series_rows.sort(key=lambda row: (row["x_pixel"], row["y_pixel"]))
        rows.extend(series_rows)
        summaries.append({"series_id": series["id"], "candidate_components": count,
                          "points": len(series_rows), "rejected_components": count - len(series_rows),
                          "coverage": len(series_rows) / count if count else 0,
                          "coverage_definition": "accepted_components / candidate_components; not detection recall"})
    return rows, summaries, rejected


def digitize(spec: dict, axes: dict, image):
    import numpy as np
    pixels = np.asarray(image.crop(tuple(spec["plot_bbox"])), dtype=np.float32)
    masks = []
    for series in spec["series"]:
        distance = pixels - np.asarray(series["rgb"], dtype=np.float32)
        masks.append(np.sum(distance * distance, axis=2) <= series.get("color_tolerance", 40) ** 2)
    overlap = np.zeros(pixels.shape[:2], dtype=np.uint8)
    for mask in masks:
        overlap += mask
    method = digitize_curves if spec["chart_type"] == "cartesian_curve" else digitize_scatter
    result = method(spec, axes, masks, overlap)
    minimum = 2 if spec["chart_type"] == "cartesian_curve" else 1
    require(any(summary["points"] >= minimum for summary in result[1]),
            "没有足够的无歧义数据点；检查颜色、Panel、标记尺寸与遮挡")
    return result


def extract(spec_path: Path, output: Path | None = None) -> dict:
    import numpy as np
    import PIL
    from PIL import ImageDraw
    output = output_path(output or spec_path.resolve().parent / "result")
    spec, source, axes, image = load_spec(spec_path)
    rows, summaries, rejected = digitize(spec, axes, image)
    scatter = spec["chart_type"] == "cartesian_scatter"
    warnings = ["数字化像素样本不是原始实验点；未插值，sigma 留空，不提供统计置信区间。",
                "叠加图仅检查几何回绘，不独立证明刻度、单位或系列正确。"]
    if any(not axis["checks"] for axis in axes.values()):
        warnings.append("部分轴没有未参与标定的独立刻度检查。")
    if any(summary.get("ambiguous_columns", 0) for summary in summaries):
        warnings.append("多轨迹/粗线/颜色重叠列已跳过，不能宣称全曲线提取。")
    if scatter:
        warnings.append("散点由已选颜色的八连通标记质心估计；接触点可能合并，计数不等于原始观测数或检测召回率。")
    if any(summary.get("rejected_components", 0) for summary in summaries):
        warnings.append("部分散点候选因尺寸、边缘截断或颜色歧义被拒绝；查看候选质量记录。")
    if any(summary["points"] < (1 if scatter else 2) for summary in summaries):
        warnings.append("部分目标系列缺少足够点，交付为不完整提取。")
    left, top, right, bottom = spec["plot_bbox"]
    overlay = image.crop((left, top, right, bottom))
    drawing = ImageDraw.Draw(overlay)
    for candidate in rejected:
        bounds = candidate["pixel_bbox"]
        drawing.rectangle((bounds[0] - left, bounds[1] - top,
                           bounds[2] - left - 1, bounds[3] - top - 1), outline="#ff8800")
    for row in rows:
        x_pixel, y_pixel = row["x_pixel"] - left, row["y_pixel"] - top
        drawing.ellipse((x_pixel - 1, y_pixel - 1, x_pixel + 1, y_pixel + 1), fill="#00cc66", outline="black")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.mkdir()
    try:
        with (output / "reference.csv").open("w", encoding="utf-8", newline="") as stream:
            writer = csv.DictWriter(stream, fieldnames=CSV_FIELDS)
            writer.writeheader()
            writer.writerows(rows)
        overlay.save(output / "digitization_overlay.png")
        write_json(output / "spec.json", spec)
        write_json(output / "source.json", source)
        write_json(output / "quality_records.json", {
            "schema": "image-data-quality-v1", "source_sha256": source["source_sha256"],
            "spec_sha256": digest((output / "spec.json").read_bytes()),
            "coordinate_space": "EXIF-normalized-image-pixels", "accepted_points": len(rows),
            "rejected_candidates": rejected})
        report = ["# 图片数据提取报告", "", "状态：needs_review（未核验回绘叠加图，不是科学复现）。", "",
                  f"源图：{source['source_path']}", f"源 SHA-256：{source['source_sha256']}", "",
                  "## 系列覆盖", ""]
        if scatter:
            report.extend(f"- {summary['series_id']}: {summary['points']}/{summary['candidate_components']} 个候选标记；"
                          f"拒绝 {summary['rejected_components']} 个（比例不是检测召回率）。" for summary in summaries)
        else:
            report.extend(f"- {summary['series_id']}: {summary['points']}/{summary['sampled_columns']} 列；"
                          f"空列 {summary['empty_columns']}，歧义列 {summary['ambiguous_columns']}。" for summary in summaries)
        report.extend(["", "绿色点为接受样本，橙色框为拒绝候选；原因见 quality_records.json。"])
        report.extend(["", "## 限制与待核验", ""] + ["- " + warning for warning in warnings])
        (output / "extraction_report.md").write_text("\n".join(report) + "\n", encoding="utf-8")
        metadata = {"schema": "image-data-result-v1", "status": "needs_review", "source": source,
                    "workflow_status": "completed", "chart_type": spec["chart_type"],
                    "algorithm": "rgb-connected-marker-v1" if scatter else "rgb-single-band-column-v2",
                    "implementation_sha256": digest(Path(__file__).read_bytes()),
                    "runtime": {"python": sys.version.split()[0], "pillow": PIL.__version__, "numpy": np.__version__},
                    "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
                    "spec_sha256": digest((output / "spec.json").read_bytes()), "axes": axes,
                    "plot_bbox": spec["plot_bbox"], "series": spec["series"], "series_summary": summaries,
                    "calibration_review": spec["calibration_review"], "overlay_review": "pending",
                    "coordinate_space": "EXIF-normalized-image-pixels", "overlay_space": "plot_crop",
                    "sampling": ({"kind": "raster_markers", "connectivity": 8, "interpolation": "none"} if scatter
                                 else {"kind": "raster_columns", "column_step": spec.get("column_step", 1), "interpolation": "none"}),
                    "uncertainty": "unknown; pixel extent is geometry, not statistical sigma",
                    "warnings": warnings,
                    "artifact_sha256": {name: digest((output / name).read_bytes()) for name in
                                        ("reference.csv", "digitization_overlay.png", "spec.json", "source.json", "extraction_report.md", "quality_records.json")}}
        if scatter:
            from scipy import __version__ as scipy_version
            metadata["runtime"]["scipy"] = scipy_version
        write_json(output / "reference.metadata.json", metadata)
        return {"status": "needs_review", "workflow_status": "completed", "output": str(output), "source_path": source["source_path"],
                "files": {name: str(output / name) for name in metadata["artifact_sha256"]}
                         | {"reference.metadata.json": str(output / "reference.metadata.json")},
                "series_summary": summaries, "warnings": warnings}
    except Exception:
        shutil.rmtree(output)
        raise


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    commands = result.add_subparsers(dest="command", required=True)
    command = commands.add_parser("prepare", help="规范化图片并交接宿主确认坐标与系列")
    command.add_argument("image", type=Path)
    command.add_argument("--output", required=True, type=Path)
    command.add_argument("--source-locator", default="")
    for name in ("check", "extract"):
        command = commands.add_parser(name)
        command.add_argument("--spec", required=True, type=Path)
        if name == "extract":
            command.add_argument("--output", type=Path)
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        if args.command == "prepare":
            result = prepare(args.image, args.output, args.source_locator)
        elif args.command == "check":
            spec, source, axes, image = load_spec(args.spec)
            result = {"status": "valid", "source_path": source["source_path"], "axes": axes}
        else:
            result = extract(args.spec, args.output)
        print(json.dumps(result, ensure_ascii=False, allow_nan=False))
        return 0
    except (ValueError, OSError, KeyError, TypeError, OverflowError, ImportError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    sys.exit(main())
