"""Bounded read-only path checks; directory presence is never data validation."""
from pathlib import Path


def inspect_candidates(root, contract):
    root = Path(root).resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Dataset root must be a directory")
    entries = []
    for dataset in contract["datasets"]:
        directories = []
        for name in dataset["directories"]:
            path = (root / name).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"Dataset path escapes root: {name}")
            directories.append({"name": name, "exists": path.is_dir()})
        entries.append({"name": dataset["name"], "split": dataset["split"],
                        "directories": directories})
    return {"root": str(root), "status": "unverified", "datasets": entries,
            "content_manifest": None, "effective_samples": None,
            "note": "Presence only; image/label contents, filters and user registration are unverified."}
