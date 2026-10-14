"""Fixed, bounded local validation recipes and read-only run provenance."""
from __future__ import annotations

import argparse
import hashlib
import importlib.metadata
import importlib.util
import itertools
import json
import math
import os
import re
import stat
import subprocess
import sys
import time
import uuid
import zipfile
from datetime import datetime, timezone
from pathlib import Path

import yaml


REPO = Path(__file__).resolve().parent.parent
RECIPE = "tt-gradient-check-v1"
SOURCE_FILES = {
    "computing/code-library/src/tensor_train.py": "snapshots/tensor_train.py",
    ".scripts/code_run.py": "snapshots/code_run.py",
}
MAX_FILE_BYTES = 16 * 1024 * 1024
MAX_TOTAL_BYTES = 64 * 1024 * 1024
HASH_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
RUN_PATTERN = re.compile(r"[a-z0-9][a-z0-9_-]{0,100}\Z")
CORE_SHAPES = ((1, 2, 3, 2), (2, 3, 2, 2), (2, 2, 2, 1))
VALIDATION_FIELDS = {"objective", "epsilon", "atol", "rtol", "checks", "passed"}


class RunError(ValueError):
    pass


def repo_path(value):
    if not isinstance(value, str) or not value or "\\" in value:
        raise RunError("无效公共相对路径")
    relative = Path(value)
    if not relative.parts or relative.is_absolute() or ".." in relative.parts or relative.parts[0] == "private":
        raise RunError("不允许 private、绝对路径或路径穿越")
    target = REPO
    for part in relative.parts:
        target = target / part
        if target.is_symlink():
            raise RunError("运行路径不允许软链接")
    try:
        target.resolve().relative_to(REPO.resolve())
    except ValueError:
        raise RunError("路径越出仓库") from None
    return target


def file_bytes(path, limit=MAX_FILE_BYTES):
    if not stat.S_ISREG(path.stat().st_mode) or path.stat().st_size > limit:
        raise RunError("产物不是普通文件或超过大小预算")
    payload = path.read_bytes()
    if len(payload) > limit:
        raise RunError("文件超过大小预算")
    return payload


def digest(path):
    return hashlib.sha256(file_bytes(path)).hexdigest()


def unique_json(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise RunError("JSON 不允许重复键")
        result[key] = value
    return result


def read_json(path):
    return json.loads(file_bytes(path, 128 * 1024).decode("utf-8"), object_pairs_hook=unique_json,
                      parse_constant=lambda value: (_ for _ in ()).throw(RunError(f"无效 JSON 数值 {value}")))


def write_json(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def project_directory(project, require_compute=False):
    if not isinstance(project, str) or not project.strip() or len(project) > 100 or project in {".", ".."} or "/" in project or "\\" in project:
        raise RunError("project 必须是单个公共项目名")
    directory = repo_path(f"projects/{project}")
    schema_path = repo_path(f"projects/{project}/schema.yaml")
    if not directory.is_dir() or not schema_path.is_file():
        raise RunError("必须使用已初始化的公共研究项目")
    schema = yaml.safe_load(file_bytes(schema_path, 128 * 1024).decode("utf-8"))
    if not isinstance(schema, dict) or not isinstance(schema.get("project"), dict) or schema["project"].get("type") != "research":
        raise RunError("目标不是 research 项目")
    if require_compute:
        compute = schema.get("compute", {})
        if not isinstance(compute, dict) or compute.get("policy") != "local" or compute.get("backend") != "local":
            raise RunError("固定 CPU 配方只接受明确的 local 计算策略")
    outputs = repo_path(f"projects/{project}/outputs")
    if not outputs.is_dir():
        raise RunError("研究项目缺少 outputs 目录")
    return directory


def run_directory(project, run_id):
    project_directory(project)
    if not isinstance(run_id, str) or not RUN_PATTERN.fullmatch(run_id):
        raise RunError("无效 run_id")
    return repo_path(f"projects/{project}/outputs/code-runs/{run_id}")


def inspect_record(path, check_current=True):
    relative = path.relative_to(REPO).as_posix()
    parts = Path(relative).parts
    if len(parts) != 6 or parts[0] != "projects" or parts[2:4] != ("outputs", "code-runs") or parts[-1] != "metadata.json":
        raise RunError("回执必须来自项目 outputs/code-runs/<run_id>/metadata.json")
    path = repo_path(relative)
    record = read_json(path)
    required = {"schema", "run_id", "project", "recipe", "status", "scientific_status", "execution_completed",
                "started_at", "finished_at", "duration_seconds", "command", "dependencies", "parameters",
                "source_bindings", "artifacts", "validation", "error"}
    if not isinstance(record, dict) or set(record) != required:
        raise RunError("运行回执字段无效")
    if record["schema"] != "scientific-run-v1" or record["recipe"] != RECIPE:
        raise RunError("未知运行回执协议或配方")
    if not isinstance(record["run_id"], str) or record["project"] != parts[1] or record["run_id"] != parts[4] or not RUN_PATTERN.fullmatch(record["run_id"]):
        raise RunError("运行身份与目录不一致")
    if not isinstance(record["status"], str) or record["status"] not in {"passed", "failed", "error", "timeout"} or record["scientific_status"] != "local_check_not_paper_reproduction":
        raise RunError("无效运行状态或科学边界")
    if not isinstance(record["execution_completed"], bool) or record["execution_completed"] != (record["status"] in {"passed", "failed"}):
        raise RunError("执行完成状态不一致")
    if not isinstance(record["source_bindings"], dict) or set(record["source_bindings"]) != set(SOURCE_FILES):
        raise RunError("配方源码绑定不完整或越出白名单")
    artifacts = record["artifacts"]
    if not isinstance(artifacts, dict) or not 7 <= len(artifacts) <= 32:
        raise RunError("产物清单无效")
    required_artifacts = {"spec.json", "inputs.npz", "stdout.log", "stderr.log", "report.md", *SOURCE_FILES.values()}
    if record["execution_completed"]:
        required_artifacts.update({"metrics.json", "results.npz"})
    if not required_artifacts.issubset(artifacts):
        raise RunError("运行产物清单缺少必需项")
    total_bytes = 0
    for name, expected in artifacts.items():
        if not isinstance(name, str) or not name or Path(name).is_absolute() or ".." in Path(name).parts or "\\" in name:
            raise RunError("产物清单含路径穿越")
        if not isinstance(expected, str) or not HASH_PATTERN.fullmatch(expected):
            raise RunError("产物缺少合法 SHA-256")
        artifact_path = repo_path(f"{path.parent.relative_to(REPO).as_posix()}/{name}")
        payload = file_bytes(artifact_path)
        total_bytes += len(payload)
        if total_bytes > MAX_TOTAL_BYTES or hashlib.sha256(payload).hexdigest() != expected:
            raise RunError("产物哈希不一致或超过总预算")
    spec = read_json(path.parent / "spec.json")
    if not isinstance(spec, dict) or spec.get("recipe") != record["recipe"] or spec.get("parameters") != record["parameters"] or spec.get("source_bindings") != record["source_bindings"]:
        raise RunError("配置与回执不一致")
    if spec.get("inputs_sha256") != artifacts["inputs.npz"]:
        raise RunError("运行前输入绑定与实际产物不一致")
    validation = record["validation"]
    if record["execution_completed"]:
        if not isinstance(validation, dict) or set(validation) != VALIDATION_FIELDS:
            raise RunError("验证结果字段无效")
        metrics = read_json(path.parent / "metrics.json")
        if metrics != validation or not isinstance(validation["checks"], list) or not validation["checks"]:
            raise RunError("回执与实际指标产物不一致")
        if not isinstance(validation["passed"], bool) or validation["passed"] != (record["status"] == "passed"):
            raise RunError("验证通过状态不一致")
        if any(not isinstance(check, dict) or not isinstance(check.get("passed"), bool) for check in validation["checks"]):
            raise RunError("检查状态无效")
        if [check.get("name") for check in validation["checks"]] != ["core_0", "core_1", "core_2", "input", "bias", "forward"]:
            raise RunError("配方检查项目不完整")
        for check in validation["checks"]:
            scaled = check.get("max_scaled_error")
            if scaled is not None and (type(scaled) not in {int, float} or not math.isfinite(scaled) or scaled < 0):
                raise RunError("误差指标无效")
            if check["passed"] != (scaled is not None and scaled <= 1):
                raise RunError("误差指标与检查结论不一致")
        if all(check["passed"] for check in validation["checks"]) != validation["passed"]:
            raise RunError("单项检查与总结果不一致")
    elif validation != {}:
        raise RunError("未完成运行不能带成功验证结果")
    changes = []
    for source_path, snapshot_name in SOURCE_FILES.items():
        binding = record["source_bindings"][source_path]
        if not isinstance(binding, dict) or set(binding) != {"sha256", "snapshot"} or binding["snapshot"] != snapshot_name:
            raise RunError("源码快照绑定无效")
        if binding["sha256"] != artifacts[snapshot_name]:
            raise RunError("源码快照与绑定哈希不一致")
        if check_current:
            try:
                current_hash = digest(repo_path(source_path))
            except OSError:
                current_hash = "missing"
            if current_hash != binding["sha256"]:
                changes.append({"path": source_path, "recorded_sha256": binding["sha256"], "current_sha256": current_hash})
    dependency_changes = []
    try:
        numpy_version = importlib.metadata.version("numpy")
    except importlib.metadata.PackageNotFoundError:
        numpy_version = "missing"
    current_dependencies = {"python": sys.version.split()[0], "numpy": numpy_version}
    if not isinstance(record["dependencies"], dict) or set(record["dependencies"]) != set(current_dependencies):
        raise RunError("依赖版本记录无效")
    if check_current:
        dependency_changes = [name for name in current_dependencies if current_dependencies[name] != record["dependencies"][name]]
    return {"status": "stale" if changes or dependency_changes else "current", "snapshot_integrity": "current",
            "source_changes": changes, "dependency_changes": dependency_changes, "record": record,
            "notice": "仅检查版本与产物完整性，不复算、不认证科学结论"}


def independent_matrix(cores):
    import numpy as np
    output_shape = [core.shape[1] for core in cores]
    input_shape = [core.shape[2] for core in cores]
    matrix = np.empty((int(np.prod(output_shape)), int(np.prod(input_shape))))
    for output_indices in itertools.product(*(range(size) for size in output_shape)):
        for input_indices in itertools.product(*(range(size) for size in input_shape)):
            product = np.ones((1, 1))
            for core, output_index, input_index in zip(cores, output_indices, input_indices):
                product = product @ core[:, output_index, input_index, :]
            matrix[np.ravel_multi_index(output_indices, output_shape), np.ravel_multi_index(input_indices, input_shape)] = product[0, 0]
    return matrix


def finite_difference(array, objective, epsilon):
    import numpy as np
    reference = np.empty_like(array)
    for index in np.ndindex(array.shape):
        original = array[index]
        try:
            array[index] = original + epsilon
            upper = objective()
            array[index] = original - epsilon
            lower = objective()
        finally:
            array[index] = original
        reference[index] = (upper - lower) / (2 * epsilon)
    return reference


def gradient_check(module, cores, vector, bias, target):
    import numpy as np
    epsilon, atol, rtol = 1e-6, 1e-7, 1e-5
    reference_output = independent_matrix(cores) @ vector + bias
    output = module.tt_matvec(cores, vector, bias)
    gradients = module.tt_vjp(cores, vector, output - target)

    def objective():
        residual = independent_matrix(cores) @ vector + bias - target
        return float(0.5 * (residual @ residual))

    references = [finite_difference(core, objective, epsilon) for core in cores]
    references.extend([finite_difference(vector, objective, epsilon), finite_difference(bias, objective, epsilon)])
    calculated = [*gradients["cores"], gradients["input"], gradients["bias"]]
    checks = []
    for name, actual, expected in zip([f"core_{index}" for index in range(len(cores))] + ["input", "bias", "forward"],
                                      [*calculated, output], [*references, reference_output]):
        absolute = np.abs(actual - expected)
        scaled = absolute / (atol + rtol * np.abs(expected))
        finite = np.isfinite(actual).all() and np.isfinite(expected).all()
        checks.append({"name": name, "element_count": int(actual.size),
                       "max_absolute_error": float(absolute.max()) if finite else None,
                       "max_scaled_error": float(scaled.max()) if finite else None,
                       "passed": bool(finite and np.all(scaled <= 1))})
    validation = {"objective": "0.5 * sum((W @ x + b - target)^2); local check, not paper classification loss",
                  "epsilon": epsilon, "atol": atol, "rtol": rtol, "checks": checks,
                  "passed": all(check["passed"] for check in checks)}
    results = {"output": output, "reference_output": reference_output,
               "input_gradient": gradients["input"], "bias_gradient": gradients["bias"]}
    for index, gradient in enumerate(gradients["cores"]):
        results[f"core_gradient_{index}"] = gradient
    for index, reference in enumerate(references):
        results[f"finite_difference_{index}"] = reference
    return validation, results


def worker(packet):
    import numpy as np
    if not isinstance(packet, dict) or set(packet) != {"project", "run_id", "source_bindings", "inputs_sha256"}:
        raise RunError("无效 worker 请求")
    directory = run_directory(packet["project"], packet["run_id"])
    bindings = packet["source_bindings"]
    if not isinstance(bindings, dict) or set(bindings) != set(SOURCE_FILES):
        raise RunError("worker 源码绑定无效")
    for source_path, snapshot in SOURCE_FILES.items():
        if bindings[source_path].get("snapshot") != snapshot or digest(directory / snapshot) != bindings[source_path].get("sha256"):
            raise RunError("worker 快照哈希不一致")
    if digest(Path(__file__)) != bindings[".scripts/code_run.py"]["sha256"]:
        raise RunError("运行器版本在启动前改变")
    if digest(repo_path(f"{directory.relative_to(REPO).as_posix()}/inputs.npz")) != packet["inputs_sha256"]:
        raise RunError("运行前输入哈希改变")
    source_path = repo_path(f"{directory.relative_to(REPO).as_posix()}/snapshots/tensor_train.py")
    specification = importlib.util.spec_from_file_location("tt_checked_snapshot", source_path)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    expected_shapes = {**{f"core_{index}.npy": shape for index, shape in enumerate(CORE_SHAPES)},
                       "vector.npy": (12,), "bias.npy": (12,), "target.npy": (12,)}
    with zipfile.ZipFile(directory / "inputs.npz") as archive:
        if set(archive.namelist()) != set(expected_shapes) or sum(item.file_size for item in archive.infolist()) > 64 * 1024:
            raise RunError("输入归档超过固定预算")
        for name, shape in expected_shapes.items():
            with archive.open(name) as stream:
                version = np.lib.format.read_magic(stream)
                reader = {(1, 0): np.lib.format.read_array_header_1_0, (2, 0): np.lib.format.read_array_header_2_0}.get(version)
                if reader is None:
                    raise RunError("不支持的数组协议")
                actual_shape, _, dtype = reader(stream)
                if actual_shape != shape or dtype != np.dtype("float64"):
                    raise RunError("输入形状或 dtype 越出固定预算")
    with np.load(directory / "inputs.npz", allow_pickle=False) as archive:
        if set(archive.files) != {"core_0", "core_1", "core_2", "vector", "bias", "target"}:
            raise RunError("配方输入不完整")
        cores = [archive[f"core_{index}"].copy() for index in range(3)]
        vector, bias, target = (archive[name].copy() for name in ("vector", "bias", "target"))
    if [core.shape for core in cores] != list(CORE_SHAPES) or any(value.shape != (12,) for value in (vector, bias, target)):
        raise RunError("配方输入超过固定形状预算")
    if not all(np.isfinite(value).all() for value in [*cores, vector, bias, target]):
        raise RunError("配方输入必须有限")
    validation, results = gradient_check(module, cores, vector, bias, target)
    if digest(directory / "inputs.npz") != packet["inputs_sha256"]:
        raise RunError("运行期间输入改变")
    np.savez(directory / "results.npz", **results)
    write_json(directory / "metrics.json", validation)
    return {"passed": validation["passed"]}


def execute_worker(project, run_id, bindings, inputs_hash, timeout):
    environment = {name: os.environ[name] for name in ("PATH", "HOME", "TMPDIR", "LANG", "SYSTEMROOT") if name in os.environ}
    environment.update(OMP_NUM_THREADS="1", OPENBLAS_NUM_THREADS="1", VECLIB_MAXIMUM_THREADS="1", PYTHONDONTWRITEBYTECODE="1")
    return subprocess.run([sys.executable, str(REPO / ".scripts/code_run.py"), "_worker"],
                          input=json.dumps({"project": project, "run_id": run_id, "source_bindings": bindings, "inputs_sha256": inputs_hash}),
                          text=True, capture_output=True, cwd=REPO, env=environment, timeout=timeout)


def run_validation(project, seed=41, timeout=60):
    import numpy as np
    import code_library
    if type(seed) is not int or not 0 <= seed < 2 ** 32 or type(timeout) not in {int, float} or not math.isfinite(timeout) or not 1 <= timeout <= 120:
        raise RunError("seed 必须为 uint32，timeout 必须在 1–120 秒")
    project_directory(project, require_compute=True)
    catalog = code_library.load_catalog()
    for evidence_id in ("code.tt_matvec", "code.tt_vjp"):
        if evidence_id not in catalog["evidence"]:
            raise RunError("梯度能力未登记")
        inspection = code_library.inspect_evidence(evidence_id, catalog["evidence"][evidence_id])
        if inspection["status"] != "current":
            raise RunError("源码与能力目录不一致；不得自动刷新绑定后执行")
    source_payloads = {source_path: file_bytes(repo_path(source_path)) for source_path in SOURCE_FILES}
    if hashlib.sha256(source_payloads["computing/code-library/src/tensor_train.py"]).hexdigest() != catalog["evidence"]["code.tt_vjp"]["sha256"]:
        raise RunError("源码在 preflight 后改变")
    run_id = f"tt-gradient-check-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S%f').lower()}-{uuid.uuid4().hex[:8]}"
    parent = repo_path(f"projects/{project}/outputs/code-runs")
    parent.mkdir(exist_ok=True)
    directory = run_directory(project, run_id)
    directory.mkdir(exist_ok=False)
    (directory / "snapshots").mkdir()
    bindings = {}
    for source_path, snapshot in SOURCE_FILES.items():
        payload = source_payloads[source_path]
        (directory / snapshot).write_bytes(payload)
        bindings[source_path] = {"sha256": hashlib.sha256(payload).hexdigest(), "snapshot": snapshot}
    parameters = {"seed": seed, "core_shapes": [list(shape) for shape in CORE_SHAPES],
                  "dtype": "float64", "index_order": "C", "timeout_seconds": timeout}
    random = np.random.default_rng(seed)
    inputs = {f"core_{index}": random.normal(scale=0.3, size=shape) for index, shape in enumerate(CORE_SHAPES)}
    inputs.update(vector=random.normal(size=12), bias=random.normal(scale=0.1, size=12), target=random.normal(size=12))
    np.savez(directory / "inputs.npz", **inputs)
    inputs_hash = digest(directory / "inputs.npz")
    write_json(directory / "spec.json", {"recipe": RECIPE, "parameters": parameters, "source_bindings": bindings,
                                         "inputs_sha256": inputs_hash})
    started_at = datetime.now(timezone.utc).isoformat()
    started = time.monotonic()
    status, validation, error = "error", {}, ""
    stdout, stderr = "", ""
    try:
        completed = execute_worker(project, run_id, bindings, inputs_hash, timeout)
        stdout, stderr = completed.stdout, completed.stderr
        if completed.returncode != 0:
            error = f"worker exit {completed.returncode}: {stderr[-600:]}"
        else:
            summary = json.loads(stdout)
            validation = read_json(directory / "metrics.json")
            if summary.get("passed") is not validation.get("passed"):
                raise RunError("worker 结果与指标文件不一致")
            status = "passed" if validation["passed"] else "failed"
    except subprocess.TimeoutExpired as exc:
        status, error = "timeout", "固定配方超过时间预算；未完成验证"
        stdout = exc.stdout.decode("utf-8", errors="replace") if isinstance(exc.stdout, bytes) else exc.stdout or ""
        stderr = exc.stderr.decode("utf-8", errors="replace") if isinstance(exc.stderr, bytes) else exc.stderr or ""
    except (ValueError, OSError) as exc:
        status, validation, error = "error", {}, str(exc)[:600]
    (directory / "stdout.log").write_text(stdout, encoding="utf-8")
    (directory / "stderr.log").write_text(stderr, encoding="utf-8")
    (directory / "report.md").write_text(
        f"# TT 梯度局部检查\n\n- run_id: {run_id}\n- status: {status}\n- seed: {seed}\n"
        "- 源码、输入、依赖和验证阈值见 metadata.json/spec.json。\n"
        "- 独立逐元素矩阵与中心有限差分检查 core、input、bias；不是 MNIST 训练或论文性能复现。\n"
        f"- error: {error or 'none'}\n", encoding="utf-8")
    artifacts = {path.relative_to(directory).as_posix(): digest(path) for path in sorted(directory.rglob("*")) if path.is_file() and path.suffix != ".pyc"}
    record = {"schema": "scientific-run-v1", "run_id": run_id, "project": project, "recipe": RECIPE,
              "status": status, "scientific_status": "local_check_not_paper_reproduction",
              "execution_completed": status in {"passed", "failed"}, "started_at": started_at,
              "finished_at": datetime.now(timezone.utc).isoformat(), "duration_seconds": time.monotonic() - started,
              "command": [sys.executable, str(REPO / ".scripts/code_run.py"), "tt-gradient-check", "--project", project,
                          "--seed", str(seed), "--timeout", str(timeout)],
              "dependencies": {"python": sys.version.split()[0], "numpy": np.__version__}, "parameters": parameters,
              "source_bindings": bindings, "artifacts": artifacts, "validation": validation, "error": error}
    write_json(directory / "metadata.json", record)
    return {"schema": "scientific-run-result-v1", "ok": status == "passed", "status": status,
            "run_id": run_id, "metadata_path": (directory / "metadata.json").relative_to(REPO).as_posix(),
            "validation": validation, "scientific_status": record["scientific_status"], "error": error}


def parser():
    argument_parser = argparse.ArgumentParser(description="显式执行固定 CPU 检查或只读检查运行回执，不是通用安全沙箱")
    commands = argument_parser.add_subparsers(dest="action", required=True)
    run_parser = commands.add_parser("tt-gradient-check")
    run_parser.add_argument("--project", required=True)
    run_parser.add_argument("--seed", type=int, default=41)
    run_parser.add_argument("--timeout", type=float, default=60)
    inspect_parser = commands.add_parser("inspect")
    inspect_parser.add_argument("--project", required=True)
    inspect_parser.add_argument("--run-id", required=True)
    return argument_parser


def main(argv=None):
    if (argv if argv is not None else sys.argv[1:]) == ["_worker"]:
        print(json.dumps(worker(json.load(sys.stdin))))
        return 0
    args = parser().parse_args(argv)
    try:
        if args.action == "tt-gradient-check":
            result = run_validation(args.project, args.seed, args.timeout)
        else:
            result = inspect_record(run_directory(args.project, args.run_id) / "metadata.json")
            result["ok"] = result["status"] == "current"
    except (RunError, OSError, ValueError, yaml.YAMLError) as exc:
        result = {"ok": False, "status": "error", "error": str(exc)[:600]}
    print(json.dumps(result, ensure_ascii=False, allow_nan=False))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
