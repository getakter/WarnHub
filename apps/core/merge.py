"""Layered configuration merge: dicts merge recursively, scalars/lists replace, None = inherit."""
import copy


def deep_merge(base, override):
    result = copy.deepcopy(base) if isinstance(base, dict) else {}
    for key, value in (override or {}).items():
        if value is None:
            continue
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _leaves(data, prefix=""):
    for key, value in (data or {}).items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict) and value:
            yield from _leaves(value, path)
        elif value is not None:
            yield path, value


def merge_layers(layers):
    """layers: [(layer_name, dict), ...] lowest priority first -> (merged, {path: layer_name})."""
    merged, provenance = {}, {}
    for name, data in layers:
        merged = deep_merge(merged, data or {})
        for path, _value in _leaves(data):
            # A deeper layer that replaces a whole dict/list owns every path beneath it.
            for existing in [p for p in provenance if p.startswith(path + ".")]:
                provenance[existing] = name
            provenance[path] = name
    return merged, provenance


def prune(data):
    """Drop None / '' / empty dicts so a sparse override only contains what was really set."""
    if isinstance(data, dict):
        out = {}
        for key, value in data.items():
            value = prune(value)
            if value is None or value == "" or value == {}:
                continue
            out[key] = value
        return out
    return data


def get_path(data, path, default=None):
    node = data
    for part in path.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node
