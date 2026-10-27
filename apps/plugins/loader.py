"""Loads built-in plugins, modules listed in EWCE_PLUGIN_MODULES and every custom_plugins/*.py."""
import importlib
import importlib.util
import logging
import sys
import traceback
from pathlib import Path

from django.conf import settings

from .registry import registry

logger = logging.getLogger("ewce.plugins")

BUILTIN_MODULES = [
    "apps.acquisition.fetchers",
    "apps.extraction.parsers",
    "apps.extraction.rules",
    "apps.extraction.fields",
    "apps.mailbox.protocols",
]

_loaded = False


def _import_module(dotted):
    try:
        importlib.import_module(dotted)
    except ModuleNotFoundError as exc:
        if exc.name == dotted:  # module not written yet / removed: tolerate, record
            registry.load_errors.append((dotted, f"module not found: {exc}"))
            return
        raise


def _load_file(path):
    module_name = f"ewce_custom_plugins.{path.stem}"
    try:
        spec = importlib.util.spec_from_file_location(module_name, path)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_name] = module
        spec.loader.exec_module(module)
        logger.info("loaded custom plugin file %s", path)
    except Exception:  # noqa: BLE001 - a broken custom plugin must not take the app down
        registry.load_errors.append((str(path), traceback.format_exc(limit=3)))
        logger.exception("failed to load custom plugin %s", path)


def load_all(force=False):
    global _loaded
    if _loaded and not force:
        return registry
    for dotted in BUILTIN_MODULES:
        _import_module(dotted)
    for dotted in settings.EWCE_PLUGIN_MODULES:
        try:
            importlib.import_module(dotted)
        except Exception:  # noqa: BLE001
            registry.load_errors.append((dotted, traceback.format_exc(limit=3)))
    for directory in settings.EWCE_CUSTOM_PLUGIN_DIRS:
        for path in sorted(Path(directory).glob("*.py")):
            if not path.name.startswith("_"):
                _load_file(path)
    _loaded = True
    return registry
