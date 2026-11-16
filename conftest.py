"""按 MaiBot PluginLoader 的合成包方式加载入口，而非依赖安装目录名。"""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "_breeze_test_plugin",
    ROOT / "plugin.py",
    submodule_search_locations=[str(ROOT)],
)
assert SPEC is not None and SPEC.loader is not None
PLUGIN = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = PLUGIN
SPEC.loader.exec_module(PLUGIN)
