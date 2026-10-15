#!/usr/bin/env python3
"""Host-authored illustration briefs; reuse bounded native and explicit raster engines."""
from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import shlex
import sys
import tempfile
import uuid
from pathlib import Path

import agent_task
import comic_generation as comic
import presentation_artwork as native

REPO = Path(__file__).resolve().parent.parent
SCHEMA = "illustration-brief-v1"
FIELDS = {"schema", "audience", "message", "analogy", "boundaries", "style",
          "placement", "caption", "alt_text", "sources", "sensitivity", "prompt", "artwork"}
PROTOCOL = {
    "name": SCHEMA,
    "fields": sorted(FIELDS),
    "rules": [
        "Host determines a single message, audience and placement before creating the prompt or artwork.",
        "analogy is optional: [] or a list of {visual,meaning}; boundaries explains scientific limits.",
        "sources is a list of metadata locators, never automatically read or sent remotely.",
        "sensitivity is public or local_only; private is not supported by this entry.",
        "Raster: prompt contains only the selected drawing instructions; artwork=null.",
        "Native: prompt is empty; artwork follows presentation-artwork-v1 and the requested canvas.",
        "Check structural validity, then render explicitly; execution is not scientific or user approval.",
    ],
    "native_artwork": native.PROTOCOL,
}


def write_json(path: Path, value: dict) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def text(value, label: str, limit=4000) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValueError(f"Invalid {label}")
    return value


def strings(values, label: str) -> None:
    if not isinstance(values, list) or not 1 <= len(values) <= 30:
        raise ValueError(f"Invalid {label} list")
    for value in values:
        text(value, label)


def scope(output_root: Path, document: str, asset: str, repo: Path) -> Path:
    if not isinstance(document, str) or not isinstance(asset, str):
        raise ValueError("Document and asset IDs must be strings")
    root = comic.allowed_output_root(output_root, repo)
    if "private" in root.relative_to(repo.resolve()).parts:
        raise ValueError("Private illustrations require the existing isolated comic CLI")
    comic._safe_segment(asset, "asset", ascii_only=True)
    directory = comic.output_directory(None, document, repo, root)
    return comic._checked_repository_path(directory.parent / "illustrations" / asset, repo)


def run_path(path: Path, repo: Path) -> Path:
    result = comic._checked_repository_path(path, repo)
    base = repo.resolve() / "temp" / "illustration"
    if not result.is_relative_to(base) or result.parent != base:
        raise ValueError("Expected an owned temp/illustration/<run> directory")
    if not result.name.isascii() or len(result.name) != 32 or any(char not in "0123456789abcdef" for char in result.name):
        raise ValueError("Invalid illustration run ID")
    return result


def prepare(*, output_root: Path, document: str, asset: str, medium: str,
            renderer: str, canvas: list[int], repo=REPO) -> dict:
    if medium not in {"poster", "article"} or renderer not in {"native", "raster"}:
        raise ValueError("Unsupported medium or renderer")
    if not isinstance(canvas, list) or len(canvas) != 2 or any(type(value) is not int or not 128 <= value <= 4096 for value in canvas):
        raise ValueError("Canvas must contain two integers in 128..4096")
    target = scope(output_root, document, asset, repo)
    if target.exists():
        raise ValueError("Illustration exists; choose a new asset ID")
    run = run_path(repo / "temp" / "illustration" / uuid.uuid4().hex, repo)
    run.mkdir(parents=True)
    request = {"schema": "illustration-request-v1", "output_root": str(target.parents[2].relative_to(repo)),
               "document": document, "asset": asset, "medium": medium, "renderer": renderer, "canvas": canvas}
    brief = {"schema": SCHEMA, "audience": "", "message": "", "analogy": [],
             "boundaries": [], "style": "", "placement": "", "caption": "", "alt_text": "",
             "sources": [], "sensitivity": "local_only", "prompt": "", "artwork": None}
    write_json(run / "request.json", request)
    write_json(run / "brief.json", brief)
    command = f"python3 .scripts/illustration.py {{action}} --run {shlex.quote(str(run.relative_to(repo)))}"
    task = agent_task.make_task(
        kind="illustration", transaction_id=run.name,
        inputs=[{"name": "request", "path": str((run / "request.json").relative_to(repo))}],
        outputs=[{"name": "brief", "path": str((run / "brief.json").relative_to(repo))}],
        protocol=PROTOCOL,
        commands={"check": command.format(action="check"), "commit": command.format(action="render")},
        context={"medium": medium, "renderer": renderer, "canvas": canvas,
                 "remote": "Raster render additionally requires --model and --allow-remote"})
    write_json(run / "task.json", task)
    return {"status": "prepared", "run": str(run.relative_to(repo)), "agent_task": task}


def check(run: Path, repo=REPO) -> tuple[dict, dict, Path]:
    run = run_path(run, repo)
    for name in ("request.json", "brief.json"):
        comic._checked_repository_path(run / name, repo)
        if (run / name).stat().st_size > 200000:
            raise ValueError("Illustration input too large")
    request = json.loads((run / "request.json").read_text(encoding="utf-8"))
    required = {"schema", "output_root", "document", "asset", "medium", "renderer", "canvas"}
    if not isinstance(request, dict) or set(request) != required or request["schema"] != "illustration-request-v1":
        raise ValueError("Invalid illustration request")
    if request["medium"] not in ("poster", "article") or request["renderer"] not in ("native", "raster"):
        raise ValueError("Unsupported medium or renderer")
    canvas = request["canvas"]
    if not isinstance(canvas, list) or len(canvas) != 2 or any(type(value) is not int or not 128 <= value <= 4096 for value in canvas):
        raise ValueError("Invalid canvas")
    text(request["output_root"], "output_root")
    target = scope(Path(request["output_root"]), request["document"], request["asset"], repo)
    brief = json.loads((run / "brief.json").read_text(encoding="utf-8"))
    if not isinstance(brief, dict) or set(brief) != FIELDS or brief["schema"] != SCHEMA:
        raise ValueError("Invalid illustration brief schema")
    for field in ("audience", "message", "style", "placement", "caption", "alt_text"):
        text(brief[field], field)
    for field in ("boundaries", "sources"):
        strings(brief[field], field)
    if any("private" in locator.replace("\\", "/").split("/") for locator in brief["sources"]):
        raise ValueError("Private sources are not supported by this entry")
    if brief["sensitivity"] not in ("public", "local_only"):
        raise ValueError("Explicit public/local_only sensitivity required")
    if not isinstance(brief["analogy"], list) or len(brief["analogy"]) > 20:
        raise ValueError("Invalid analogy mapping")
    for mapping in brief["analogy"]:
        if not isinstance(mapping, dict) or set(mapping) != {"visual", "meaning"}:
            raise ValueError("Invalid analogy mapping fields")
        text(mapping["visual"], "visual")
        text(mapping["meaning"], "meaning")
    if request["renderer"] == "native":
        if brief["prompt"] != "":
            raise ValueError("Native drawings do not send a raster prompt")
        native_brief = {"schema": "presentation-artwork-brief-v1", "canvas": canvas,
                        "sensitivity": brief["sensitivity"], "brief": brief["message"], "sources": brief["sources"]}
        native.validate_brief(native_brief)
        native.validate_artwork(brief["artwork"], native_brief)
    else:
        text(brief["prompt"], "prompt", 20000)
        if brief["artwork"] is not None:
            raise ValueError("Raster brief must not embed native artwork")
    return request, brief, target


def render(run: Path, *, model: str | None = None, allow_remote=False,
           dry_run=False, repo=REPO) -> dict:
    request, brief, target = check(run, repo)
    if target.exists():
        raise ValueError("Illustration exists; choose a new asset ID")
    raster = request["renderer"] == "raster"
    if raster and (brief["sensitivity"] != "public" or (not dry_run and not allow_remote)):
        raise ValueError("Remote rendering requires public sensitivity and --allow-remote")
    if raster and not model:
        raise ValueError("Raster rendering requires an explicit catalog model")
    if not raster and (allow_remote or model):
        raise ValueError("Native rendering is local; remote/model flags are not applicable")
    arguments = {"project": None, "explicit_output_root": Path(request["output_root"]),
                 "article_id": request["document"], "asset_id": request["asset"], "prompt": brief["prompt"],
                 "model": model, "size": "x".join(map(str, request["canvas"])),
                 "parameters": None, "repo": repo, "allow_remote": allow_remote, "dry_run": dry_run}
    if dry_run:
        engine = comic.generate_asset(**arguments) if raster else {"status": "dry_run"}
        return {"status": "dry_run", "target": str(target), "engine": engine}
    target.parent.mkdir(parents=True, exist_ok=True)
    lock = comic._checked_repository_path(target.parent / f".{target.name}.lock", repo)
    with lock.open("x", encoding="utf-8"):
        stage = None
        engine = None
        try:
            if target.exists():
                raise ValueError("Illustration already exists")
            stage = Path(tempfile.mkdtemp(prefix=f".{target.name}-", dir=target.parent))
            if raster:
                engine = comic.generate_asset(**arguments)
                artifacts = {engine["output_path"]: engine["sha256"]}
            else:
                svg, fragment = native.export_artwork(brief["artwork"])
                width, height = request["canvas"]
                page = f'<!doctype html><meta charset="utf-8"><body style="margin:0;position:relative;width:{width}px;height:{height}px">{fragment}</body>'
                for name, content in {"artwork.svg": svg, "artwork.fragment.html": fragment, "artwork.html": page}.items():
                    (stage / name).write_text(content, encoding="utf-8")
                artifacts = {name: hashlib.sha256((stage / name).read_bytes()).hexdigest()
                             for name in ("artwork.svg", "artwork.html", "artwork.fragment.html")}
            write_json(stage / "brief.json", brief)
            metadata = {"schema": "illustration-asset-v1", "request": request,
                        "brief_sha256": hashlib.sha256((stage / "brief.json").read_bytes()).hexdigest(),
                        "artifacts": artifacts, "engine_receipt": engine,
                        "review_status": "pending", "public_release": "not_approved",
                        "canvas_semantics": "requested_not_verified" if raster else "native_coordinates"}
            write_json(stage / "asset.json", metadata)
            stage.rename(target)
            return {"status": "rendered", "directory": str(target), "metadata": metadata}
        except Exception as exc:
            if engine is not None:
                raise RuntimeError(f"Illustration metadata publication failed; raster asset remains at {engine['output_path']}") from exc
            raise
        finally:
            if stage is not None and stage.exists():
                shutil.rmtree(stage)
            lock.unlink()


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("prepare")
    create.add_argument("--output-root", type=Path, required=True)
    create.add_argument("--document", required=True)
    create.add_argument("--asset", required=True)
    create.add_argument("--medium", choices=["poster", "article"], required=True)
    create.add_argument("--renderer", choices=["native", "raster"], required=True)
    create.add_argument("--canvas", type=int, nargs=2, required=True)
    for name in ("check", "render"):
        command = commands.add_parser(name)
        command.add_argument("--run", type=Path, required=True)
        if name == "render":
            command.add_argument("--model")
            command.add_argument("--allow-remote", action="store_true")
            command.add_argument("--dry-run", action="store_true")
    options = vars(parser.parse_args(argv))
    command = options.pop("command")
    try:
        if command == "prepare":
            result = prepare(**options)
        elif command == "check":
            request, brief, target = check(**options)
            result = {"status": "valid", "target": str(target), "review_status": "pending"}
        else:
            result = render(**options)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    except (ValueError, OSError, RuntimeError) as exc:
        print(json.dumps({"status": "error", "error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
