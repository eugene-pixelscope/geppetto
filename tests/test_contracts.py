import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from jsonschema.exceptions import ValidationError
from geppetto.configuration import compile_preview, digest
from geppetto.datasets import inspect_candidates
from tools.export_jnr import provenance

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads((ROOT / path).read_text())


class ConfigTests(unittest.TestCase):
    def setUp(self):
        self.bundle = read("contracts/uncertainty-jnr.config.json")
        self.managed = read("examples/managed.json")

    def preview(self, overrides=None):
        return compile_preview(self.bundle, overrides or {}, self.managed)

    def test_defaults_preset_override_precedence(self):
        result = self.preview({"training": {"max_epochs": 3}})
        cfg = result["config"]
        self.assertEqual(cfg["model"]["transformer_num_layers"], 1)
        self.assertEqual(cfg["loss"]["digit_aux_loss_weight"], 0.1)
        self.assertEqual(cfg["training"]["max_epochs"], 3)
        self.assertEqual(cfg["training"]["weight_decay"], 1e-4)
        self.assertFalse(cfg["wandb"]["enabled"])
        self.assertFalse(result["executable"])
        self.assertEqual(result["config_sha256"], digest(cfg))
        self.assertTrue(self.bundle["preset"]["wandb"]["enabled"])

    def test_removed_and_misspelled_fields_rejected(self):
        for changes in ({"training": {"devices": 1}}, {"training": {"max_epoch": 3}}, {"typo": {}}):
            with self.subTest(changes=changes), self.assertRaises(ValidationError):
                self.preview(changes)

    def test_no_silent_type_coercion(self):
        for value in ("10", True, None, 1.5):
            with self.subTest(value=value), self.assertRaises(ValidationError):
                self.preview({"training": {"max_epochs": value}})

    def test_literal_and_tuple_shape(self):
        for changes in ({"model": {"uncertainty_head": "bad"}}, {"data": {"target_size": [224]}}):
            with self.assertRaises(ValidationError):
                self.preview(changes)

    def test_protected_fields_and_group_replacement(self):
        for changes in ({"data": {"root_dir": "/other"}}, {"wandb": None}, {"logging": "bad"}):
            with self.assertRaises(ValueError):
                self.preview(changes)

    def test_explicit_management_required(self):
        del self.managed["wandb"]
        with self.assertRaises(ValueError):
            self.preview()

    def test_wandb_policy_and_absolute_paths(self):
        self.managed["wandb"]["enabled"] = True
        with self.assertRaises(ValueError):
            self.preview()
        self.managed["wandb"]["enabled"] = False
        self.managed["data"]["root_dir"] = "relative"
        with self.assertRaises(ValueError):
            self.preview()

    def test_schema_drift_rejected(self):
        self.bundle["schema"]["title"] = "changed"
        with self.assertRaises(ValueError):
            self.preview()

    def test_no_input_mutation_and_stable_hash(self):
        original = copy.deepcopy(self.bundle)
        a, b = self.preview(), self.preview()
        self.assertEqual(a, b)
        self.assertEqual(self.bundle, original)

    def test_core_does_not_import_model_dependencies(self):
        code = "import geppetto.cli,sys; assert not {'torch','pydantic','yaml'} & sys.modules.keys()"
        subprocess.run([sys.executable, "-c", code], cwd=ROOT, check=True)


class DatasetTests(unittest.TestCase):
    def test_presence_is_not_validation(self):
        with tempfile.TemporaryDirectory() as folder:
            contract = {"datasets": [{"name": "a", "split": "train", "directories": ["a", "b"]}]}
            (Path(folder) / "a").mkdir()
            result = inspect_candidates(folder, contract)
            self.assertEqual(result["status"], "unverified")
            self.assertIsNone(result["content_manifest"])
            self.assertEqual([x["exists"] for x in result["datasets"][0]["directories"]], [True, False])

    def test_path_escape_and_symlink_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            (Path(folder) / "escape").symlink_to("/tmp", target_is_directory=True)
            for name in ("../outside", "escape", "/etc"):
                with self.assertRaises(ValueError):
                    inspect_candidates(folder, {"datasets": [{"name": "x", "split": "train", "directories": [name]}]})


class ProvenanceTests(unittest.TestCase):
    def test_changed_commit_or_source_is_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            repo = Path(folder)
            def git(*args):
                return subprocess.check_output(["git", "-C", folder, *args], stderr=subprocess.DEVNULL).decode().strip()
            git("init")
            (repo / "config.py").write_text("# fixture\n")
            git("add", "config.py")
            git("-c", "user.name=Test", "-c", "user.email=test@example.invalid", "commit", "-m", "fixture")
            head = git("rev-parse", "HEAD")
            self.assertEqual(provenance(repo, head)["commit"], head)
            with self.assertRaises(ValueError):
                provenance(repo, "0" * 40)
            (repo / "config.py").write_text("# changed\n")
            with self.assertRaises(ValueError):
                provenance(repo, head)


if __name__ == "__main__":
    unittest.main()
