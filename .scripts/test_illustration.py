#!/usr/bin/env python3
from __future__ import annotations

import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import illustration as il


class IllustrationTest(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.repo = Path(self.temporary.name).resolve()
        self.outputs = self.repo / "projects" / "demo" / "outputs"
        self.outputs.mkdir(parents=True)

    def prepare(self, renderer="native", medium="poster", asset="sample"):
        result = il.prepare(output_root=self.outputs, document="demo", asset=asset,
                            medium=medium, renderer=renderer, canvas=[800, 400], repo=self.repo)
        self.assertEqual(result["agent_task"]["schema"], "agent-task-v1")
        run = self.repo / result["run"]
        brief = json.loads((run / "brief.json").read_text())
        self.assertEqual(brief["sensitivity"], "local_only")
        brief.update(audience="非理科读者", message="一图只讲参照范围", analogy=[],
                     boundaries=["定性示意，不是数据"], style="简明", placement="段落后",
                     caption="参照改变", alt_text="浮标与目标之间的间距", sources=["academic/raw/paper.md#L17"])
        if renderer == "native":
            brief["artwork"] = {"schema": "presentation-artwork-v1", "canvas": [800, 400],
                                "background": "#FFFFFF", "rationale": "示意参照", "objects": [
                                    {"type": "rect", "x": 20, "y": 20, "w": 80, "h": 80,
                                     "fill": "#123456", "stroke": "none", "stroke_width": 0, "radius": 5}]}
        else:
            brief.update(prompt="Draw only a buoy and a submarine. No text.", sensitivity="public")
        il.write_json(run / "brief.json", brief)
        return run, brief

    def test_native_local_export_for_both_media(self):
        for medium in ("poster", "article"):
            run, brief = self.prepare(medium=medium, asset=medium)
            original = (run / "brief.json").read_bytes()
            with mock.patch.object(il.comic, "generate_asset") as remote:
                result = il.render(run, repo=self.repo)
                remote.assert_not_called()
            target = Path(result["directory"])
            self.assertTrue((target / "artwork.svg").is_file())
            self.assertTrue((target / "artwork.html").is_file())
            self.assertEqual(json.loads((target / "brief.json").read_text()), brief)
            self.assertEqual(result["metadata"]["public_release"], "not_approved")
            self.assertEqual(original, (run / "brief.json").read_bytes())
            with self.assertRaisesRegex(ValueError, "exists"):
                il.render(run, repo=self.repo)

    def test_prepare_does_not_call_model_and_incomplete_brief_fails(self):
        with mock.patch.object(il.comic, "generate_asset") as remote:
            result = il.prepare(output_root=self.outputs, document="demo", asset="empty",
                                medium="poster", renderer="raster", canvas=[800, 400], repo=self.repo)
            remote.assert_not_called()
        with self.assertRaisesRegex(ValueError, "audience"):
            il.check(self.repo / result["run"], repo=self.repo)

    def test_raster_consent_and_selected_prompt_only(self):
        run, brief = self.prepare("raster")
        with mock.patch.object(il.comic, "generate_asset") as remote:
            with self.assertRaisesRegex(ValueError, "allow-remote"):
                il.render(run, model="candidate", repo=self.repo)
            brief["sensitivity"] = "local_only"
            il.write_json(run / "brief.json", brief)
            with self.assertRaisesRegex(ValueError, "public"):
                il.render(run, model="candidate", allow_remote=True, repo=self.repo)
            remote.assert_not_called()
            brief["sensitivity"] = "public"
            il.write_json(run / "brief.json", brief)
            remote.return_value = {"status": "complete", "output_path": "/existing/image.png", "sha256": "abc"}
            result = il.render(run, model="candidate", allow_remote=True, repo=self.repo)
            kwargs = remote.call_args.kwargs
            self.assertEqual(kwargs["prompt"], brief["prompt"])
            self.assertNotIn("sources", kwargs)
            self.assertNotIn("audience", kwargs)
            self.assertEqual(result["metadata"]["canvas_semantics"], "requested_not_verified")

    def test_dry_run_and_api_failure_publish_nothing(self):
        run, brief = self.prepare("raster")
        with mock.patch.object(il.comic, "generate_asset", return_value={"status": "dry_run"}) as remote:
            result = il.render(run, model="candidate", dry_run=True, repo=self.repo)
            self.assertTrue(remote.call_args.kwargs["dry_run"])
            self.assertFalse(Path(result["target"]).exists())
        with mock.patch.object(il.comic, "generate_asset", side_effect=il.comic.ComicGenerationError("failure")):
            with self.assertRaisesRegex(il.comic.ComicGenerationError, "failure"):
                il.render(run, model="candidate", allow_remote=True, repo=self.repo)
        self.assertFalse((self.outputs / "demo" / "illustrations" / "sample").exists())
        self.assertFalse(list((self.outputs / "demo" / "illustrations").iterdir()))

    def test_publication_failure_preserves_legacy_raster_receipt(self):
        run, brief = self.prepare("raster")
        receipt = {"status": "complete", "output_path": "/existing/image.png", "sha256": "abc"}
        with mock.patch.object(il.comic, "generate_asset", return_value=receipt), mock.patch.object(il, "write_json", side_effect=OSError("disk")):
            with self.assertRaisesRegex(RuntimeError, "raster asset remains"):
                il.render(run, model="candidate", allow_remote=True, repo=self.repo)

    def test_private_and_protected_output_rejected_before_staging(self):
        for path in ("private/outputs", "academic/raw/outputs", "academic/wiki/outputs"):
            root = self.repo / path
            root.mkdir(parents=True)
            with self.assertRaises((ValueError, il.comic.ComicGenerationError)):
                il.prepare(output_root=root, document="demo", asset="test", medium="poster",
                           renderer="native", canvas=[800, 400], repo=self.repo)
        self.assertFalse((self.repo / "temp").exists())

    def test_symlinks_input_output_and_run_escape(self):
        run, brief = self.prepare()
        (run / "brief.json").rename(run / "real.json")
        (run / "brief.json").symlink_to(run / "real.json")
        with self.assertRaisesRegex(il.comic.ComicGenerationError, "symlink"):
            il.check(run, repo=self.repo)
        alias = self.repo / "alias"
        alias.symlink_to(self.outputs, target_is_directory=True)
        with self.assertRaisesRegex(il.comic.ComicGenerationError, "symlink"):
            il.prepare(output_root=alias, document="demo", asset="test", medium="poster",
                       renderer="native", canvas=[800, 400], repo=self.repo)
        with self.assertRaises((ValueError, il.comic.ComicGenerationError)):
            il.check(self.repo / "temp" / "illustration" / "..", repo=self.repo)

    def test_native_geometry_and_brief_structure(self):
        run, original = self.prepare()
        for field, value in (("analogy", [{"visual": "sea"}]), ("sources", []), ("sensitivity", "private"),
                             ("sources", ["private/raw/source.md"]), ("prompt", "remote")):
            brief = {**original, field: value}
            il.write_json(run / "brief.json", brief)
            with self.assertRaises(ValueError):
                il.check(run, repo=self.repo)
        original["artwork"]["objects"][0]["w"] = 900
        il.write_json(run / "brief.json", original)
        with self.assertRaises(ValueError):
            il.check(run, repo=self.repo)

    def test_lock_and_existing_output_reject_before_api(self):
        run, brief = self.prepare("raster")
        request, brief, target = il.check(run, self.repo)
        target.parent.mkdir(parents=True)
        (target.parent / ".sample.lock").write_text("busy")
        with mock.patch.object(il.comic, "generate_asset") as remote:
            with self.assertRaises(FileExistsError):
                il.render(run, model="candidate", allow_remote=True, repo=self.repo)
            remote.assert_not_called()
        self.assertTrue((target.parent / ".sample.lock").exists())

    def test_raster_reuses_real_engine_manifest_and_receipts(self):
        run, brief = self.prepare("raster")
        encoded = "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Y9Zf1sAAAAASUVORK5CYII="
        config = {"COMIC_IMAGE_API_BASE": "https://provider.example/v1", "COMIC_IMAGE_API_KEY": "fake-test-key"}
        with mock.patch.object(il.comic, "require_candidate"), mock.patch.object(il.comic, "load_comic_env", return_value=config), mock.patch.object(il.comic, "call_image_api", return_value=({"data": [{"b64_json": encoded}]}, "test-request")) as network:
            result = il.render(run, model="test-model", allow_remote=True, repo=self.repo)
        images = self.outputs / "demo" / "images"
        self.assertEqual((images / "sample.png").read_bytes(), base64.b64decode(encoded))
        manifest = json.loads((images / "manifest.json").read_text())
        self.assertEqual(len(manifest["runs"]), 1)
        self.assertEqual(len(list((images / "receipts").glob("*.json"))), 1)
        self.assertEqual(network.call_args.args[2], {"model": "test-model", "prompt": brief["prompt"], "n": 1, "size": "800x400"})
        self.assertEqual(result["metadata"]["engine_receipt"]["sha256"], manifest["runs"][0]["sha256"])

    def test_request_types_and_native_remote_flags_fail_closed(self):
        run, brief = self.prepare()
        with self.assertRaisesRegex(ValueError, "local"):
            il.render(run, allow_remote=True, repo=self.repo)
        original = json.loads((run / "request.json").read_text())
        for field, value in (("medium", []), ("renderer", {}), ("document", None),
                             ("asset", []), ("canvas", [False, 400]), ("output_root", [])):
            il.write_json(run / "request.json", {**original, field: value})
            with self.assertRaises(ValueError):
                il.check(run, repo=self.repo)


if __name__ == "__main__":
    unittest.main()
