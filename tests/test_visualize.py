"""Phase 5 logic that needs no Blender, GPU or ComfyUI: model selection, cameras' inputs,
walkthrough path, enhancement workflow shape and the geometry-preservation score."""
import os
import tempfile
import unittest
from pathlib import Path

_TMP = tempfile.TemporaryDirectory()
os.environ["STUDIO_PROJECTS_ROOT"] = _TMP.name
os.environ["STUDIO_CONFIG"] = str(Path(_TMP.name) / "none.yaml")

import cv2  # noqa: E402
import numpy as np  # noqa: E402

from studio_core import comfy, config, projects, visualize  # noqa: E402
from studio_core.projects import StudioError  # noqa: E402

SPEC = {
    "levels": [{"id": "L0", "elevation_m": 0.0}, {"id": "L1", "elevation_m": 3.0}],
    "spaces": [
        {"id": "S1", "name": "Hall", "type": "entrance", "level": "L0", "area_m2": 17, "polygon": [[4, 0], [5.2, 0], [5.2, 14], [4, 14]]},
        {"id": "S2", "name": "Living", "type": "living", "level": "L0", "area_m2": 40, "polygon": [[5.2, 0], [9, 0], [9, 11], [5.2, 11]]},
        {"id": "S3", "name": "WC", "type": "wc", "level": "L0", "area_m2": 4, "polygon": [[0, 0], [4, 0], [4, 1], [0, 1]]},
        {"id": "S4", "name": "Master", "type": "master_bedroom", "level": "L1", "area_m2": 16, "polygon": [[5.2, 0], [9, 0], [9, 4], [5.2, 4]]},
        {"id": "S5", "name": "Photo room", "type": "bedroom", "level": "L0", "area_m2": 99, "polygon": []}],
    "openings": [{"id": "D1", "kind": "door", "level": "L0", "position": [4.6, 0], "width_m": 1.0},
                 {"id": "D2", "kind": "door", "level": "L0", "position": [5.2, 3], "width_m": 0.9},
                 {"id": "N1", "kind": "window", "level": "L0", "position": [7, 0], "width_m": 1.5}],
}


class Inputs(unittest.TestCase):
    def test_interiors_are_largest_habitable_rooms_with_floor_height(self):
        rooms = visualize.pick_interiors(SPEC, 2)
        self.assertEqual([(r["id"], r["level_z"]) for r in rooms], [("S2", 0.0), ("S4", 3.0)])  # no wc, no unlocated
        self.assertEqual(visualize.pick_interiors(SPEC, 0), [])

    def test_walk_path_enters_by_the_front_door_and_stays_in_the_hall(self):
        path = visualize.walk_path(SPEC)
        self.assertEqual(len(path), 5)
        self.assertTrue(all(z == 1.6 for _, _, z in path))
        self.assertLess(path[0][1], -5)                      # starts outside, south of the door at y=0
        self.assertAlmostEqual(path[0][0], 4.6, places=2)    # lined up with the front door, not D2
        ys = [y for _, y, _ in path]
        self.assertEqual(ys, sorted(ys))                     # walks steadily inward
        for x, y, _ in path[2:]:
            self.assertTrue(4 <= x <= 5.2 and 0 <= y <= 14, (x, y))   # inside the hall once through the door
        self.assertIsNone(visualize.walk_path({"levels": SPEC["levels"], "spaces": [], "openings": []}))


class ModelSelection(unittest.TestCase):
    def test_refuses_missing_and_stale_models(self):
        slug = projects.create_project("Viz House")["slug"]
        with self.assertRaises(StudioError):
            visualize.current_model(slug)                    # nothing built
        p = projects.project_dir(slug)
        blend = p / "blender" / "model_r0002.blend"
        blend.write_bytes(b"x")
        rev = projects.write_state(slug, "model_spec.json", projects.read_state(slug, "model_spec.json"))
        projects.add_artifact(slug, "model_blend", blend, "t", True, source_revision=rev)
        self.assertEqual(visualize.current_model(slug)["path"], "blender/model_r0002.blend")
        projects.write_state(slug, "model_spec.json", projects.read_state(slug, "model_spec.json"))  # plan changes
        with self.assertRaises(StudioError) as e:
            visualize.current_model(slug)
        self.assertIn("build-model again", str(e.exception))
        with self.assertRaises(StudioError):
            visualize.render(slug, preset="noon")
        with self.assertRaises(StudioError):
            visualize.render(slug, views=["underwater"])


class BusyComfy(unittest.TestCase):
    """Live bug: ComfyUI stopped answering while loading a model, an httpx.ReadTimeout escaped
    and the whole render job failed after the renders and video were already made."""

    def test_timeouts_cost_one_image_not_the_run(self):
        import httpx
        slug = projects.create_project("Busy Comfy House")["slug"]
        p = projects.project_dir(slug)
        stills = []
        for v in ("front", "corner"):
            for suffix in ("", "_lines"):
                (p / "renders" / f"{v}{suffix}.png").write_bytes(b"png")
            stills.append({"view": v, "file": f"renders/{v}.png", "lines": f"renders/{v}_lines.png"})
        calls = {"run": 0, "free": 0}

        class Fake:
            base = "http://fake"

            def __init__(self, cfg): pass
            def up(self): return True
            def upload(self, path, name): return name
            def check_nodes(self, g): return []
            def free(self): calls["free"] += 1

            def run(self, g):
                calls["run"] += 1
                raise httpx.ReadTimeout("timed out")

        real, comfy.Comfy = comfy.Comfy, Fake
        try:
            notes = []
            done = visualize._enhance(slug, p, stills, "day", {}, {}, 1, config.load(), notes)
        finally:
            comfy.Comfy = real
        self.assertEqual((done, calls["run"], calls["free"]), ([], 2, 1))   # tried both, freed VRAM once
        self.assertEqual(sum("enhancement failed" in n for n in notes), 2)


class Enhancement(unittest.TestCase):
    def test_workflow_is_wired_to_keep_geometry(self):
        cfg = config.load()
        g = comfy.workflow("a.png", "b.png", "house", cfg, 7, "x/y")
        self.assertEqual(g["4"]["class_type"], "ImageInvert")                       # black-on-white -> white-on-black
        self.assertEqual(g["8"]["inputs"]["image"], ["4", 0])                       # ControlNet sees the line pass
        self.assertEqual(g["9"]["inputs"]["pixels"], ["2", 0])                      # img2img starts from the render
        self.assertEqual(g["10"]["inputs"]["positive"], ["8", 0])                   # sampler uses controlled conditioning
        self.assertLess(g["10"]["inputs"]["denoise"], 1.0)
        self.assertIn("extra windows", g["7"]["inputs"]["text"])
        self.assertIn("dusk", comfy.prompt_for({"style": "modern"}, "dusk"))
        self.assertIn("kitchen", comfy.prompt_for({}, "day", "kitchen"))

    def test_geometry_score_separates_kept_from_altered(self):
        d = Path(tempfile.mkdtemp(dir=_TMP.name))
        lines = np.full((360, 640), 255, np.uint8)
        cv2.rectangle(lines, (150, 90), (490, 300), 0, 2)          # building outline
        for x in (200, 300, 400):
            cv2.rectangle(lines, (x, 150), (x + 50, 210), 0, 2)    # three windows
        cv2.imwrite(str(d / "lines.png"), lines)

        def photo(windows):
            rng = np.random.default_rng(1)
            img = np.full((360, 640), 150, np.uint8)
            cv2.rectangle(img, (150, 90), (490, 300), 205, -1)
            for x in windows:
                cv2.rectangle(img, (x, 150), (x + 50, 210), 60, -1)
            return np.clip(img + rng.normal(0, 4, img.shape), 0, 255).astype(np.uint8)

        cv2.imwrite(str(d / "kept.png"), photo((200, 300, 400)))
        cv2.imwrite(str(d / "moved.png"), photo((230, 440)))        # one window gone, two shifted
        cv2.imwrite(str(d / "blank.png"), np.full((360, 640), 128, np.uint8))
        cv2.imwrite(str(d / "dark.png"), (photo((200, 300, 400)) * 0.12).astype(np.uint8))  # intact but very dark
        kept = comfy.geometry_score(d / "lines.png", d / "kept.png")["edge_recall"]
        moved = comfy.geometry_score(d / "lines.png", d / "moved.png")["edge_recall"]
        blank = comfy.geometry_score(d / "lines.png", d / "blank.png")["edge_recall"]
        threshold = config.load()["comfyui"]["enhance"]["min_edge_recall"]
        self.assertGreater(kept, 0.9)
        self.assertGreater(comfy.geometry_score(d / "lines.png", d / "dark.png")["edge_recall"], 0.8)
        self.assertLess(moved, kept - 0.2)
        self.assertEqual(blank, 0.0)
        self.assertTrue(kept >= threshold > blank)
        cv2.imwrite(str(d / "white.png"), np.full((360, 640), 255, np.uint8))
        with self.assertRaises(StudioError):
            comfy.geometry_score(d / "white.png", d / "kept.png")   # a line pass with no lines proves nothing


if __name__ == "__main__":
    unittest.main()
