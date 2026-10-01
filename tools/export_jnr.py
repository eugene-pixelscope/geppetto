"""Trusted model-environment adapter. Run separately from the Geppetto service.

Imports only the reviewed config.py, never train.py or torch. Does not call the
upstream unsafe YAML loader, create model directories, or touch CUDA.
"""
import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys

import pydantic
import yaml
from jsonschema import Draft202012Validator

sys.dont_write_bytecode = True
PRESET = "configs/soccernet_full_da_paper_aug_auxdigit.yaml"
PIN = "0434562ea1497643e8230a6b25eb3d2932559f95"
MANAGED = ["data.root_dir", "data.cache_dir", "logging.base_dir",
           "logging.experiment_name", "wandb.enabled"]


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def git(repo, *args):
    return subprocess.check_output(["git", "-C", str(repo), *args])


def provenance(repo, expected):
    commit = git(repo, "rev-parse", "HEAD").decode().strip()
    if commit != expected:
        raise ValueError(f"Commit mismatch: expected {expected}, got {commit}")
    if git(repo, "status", "--porcelain", "--untracked-files=normal").strip():
        raise ValueError("Model repository is dirty; review and pin before export")
    files = {}
    # Pin the full Git tree by commit, hash only the reviewed integration inputs.
    # Dataset content has a separate manifest lifecycle and can be very large.
    paths = ["config.py", "train.py", "pyproject.toml", "configs", "src", "tools"]
    for raw in git(repo, "ls-files", "-z", "--", *paths).split(b"\0"):
        if raw:
            name = os.fsdecode(raw)
            path = repo / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Unsupported tracked file: {name}")
            files[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    return {"commit": commit, "files_sha256": files, "tree_sha256": digest(files),
            "preset": PRESET, "hash_scope": paths}


def close_objects(value):
    if isinstance(value, dict):
        # Leave intentionally open dictionaries (e.g. model_kwargs) open.
        if value.get("type") == "object" and "properties" in value:
            value["additionalProperties"] = False
        for child in value.values():
            close_objects(child)
    elif isinstance(value, list):
        for child in value:
            close_objects(child)


def export(repo, expected=PIN, resolved=None):
    repo = Path(repo).resolve(strict=True)
    source = provenance(repo, expected)
    os.environ["OCR_DIR"] = "/workspace/model"
    spec = importlib.util.spec_from_file_location("geppetto_reviewed_jnr_config", repo / "config.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    schema = module.Config.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    close_objects(schema)
    Draft202012Validator.check_schema(schema)
    raw = yaml.safe_load((repo / PRESET).read_text()) if resolved is None else resolved
    # Validate BEFORE Pydantic can ignore extras or coerce incorrect values.
    Draft202012Validator(schema).validate(raw)
    validated = module.Config.model_validate(raw).model_dump(mode="json")
    Draft202012Validator(schema).validate(validated)
    if provenance(repo, expected) != source:
        raise ValueError("Source changed during export")
    if resolved is not None:
        if validated != raw:
            raise ValueError("Runtime Config changed resolved values; freeze normalized values first")
        return {"status": "validated", "config_sha256": digest(validated), "source": source}
    return {"contract_version": "0.1", "model_id": "uncertainty-jnr",
            "source": source, "export_environment": {"python": sys.version.split()[0],
            "pydantic": pydantic.__version__, "pyyaml": yaml.__version__},
            "schema_sha256": digest(schema), "schema": schema,
            "preset": raw, "resolved_preset": validated, "managed_fields": MANAGED}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--expected-commit", default=PIN)
    parser.add_argument("--validate-config", type=Path, help="JSON preview to validate in model environment")
    args = parser.parse_args()
    resolved = None
    if args.validate_config:
        preview = json.loads(args.validate_config.read_text())
        resolved = preview["config"]
        if digest(resolved) != preview["config_sha256"]:
            parser.error("Config digest mismatch")
        if preview["source"] != provenance(args.repo, args.expected_commit):
            parser.error("Preview source mismatch")
    print(json.dumps(export(args.repo, args.expected_commit, resolved), indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
