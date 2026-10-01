"""Compile a preview from a versioned export; this never submits training."""
import copy
import hashlib
import json

from jsonschema import Draft202012Validator


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(",", ":"),
                                     allow_nan=False).encode()).hexdigest()


def merge(base, overlay):
    result = copy.deepcopy(base)
    for key, value in overlay.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def compile_preview(bundle, overrides, managed):
    """Export defaults+recipe, user overrides, then explicit managed policy."""
    if bundle.get("contract_version") != "0.1":
        raise ValueError("Unsupported contract version")
    if digest(bundle["schema"]) != bundle["schema_sha256"]:
        raise ValueError("Schema digest mismatch")
    if not isinstance(overrides, dict) or not isinstance(managed, dict):
        raise ValueError("Overrides and managed values must be objects")
    protected = bundle["managed_fields"]
    for group, value in overrides.items():
        if not isinstance(value, dict):
            if any(p.startswith(group + ".") for p in protected):
                raise ValueError(f"Cannot replace managed group: {group}")
            continue
        for field in value:
            if f"{group}.{field}" in protected:
                raise ValueError(f"Managed field cannot be overridden: {group}.{field}")
    supplied = set()
    for group, values in managed.items():
        if not isinstance(values, dict):
            raise ValueError("Managed values must use nested objects")
        supplied.update(f"{group}.{key}" for key in values)
    if supplied != set(protected):
        raise ValueError(f"Managed fields must match exactly: {protected}")
    # Phase 0 policy: W&B optional upstream; disabled in Geppetto preview.
    if managed["wandb"]["enabled"] is not False:
        raise ValueError("Phase 0 requires wandb.enabled=false")
    for path in ("data.root_dir", "data.cache_dir", "logging.base_dir"):
        group, key = path.split(".")
        value = managed[group][key]
        if not isinstance(value, str) or not value.startswith("/"):
            raise ValueError(f"Container path must be absolute: {path}")
    final = merge(merge(bundle["resolved_preset"], overrides), managed)
    Draft202012Validator(bundle["schema"]).validate(final)
    return {"kind": "configuration_preview", "executable": False,
            "source": bundle["source"], "schema_sha256": bundle["schema_sha256"],
            "config_sha256": digest(final), "config": final,
            "pending": ["runtime Config validation", "dataset content manifest",
                        "container image digest", "GPU isolation proof", "execution adapter"]}
