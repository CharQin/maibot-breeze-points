"""防止再次把支持的旧版本拦在 Manifest 校验之外。"""

import json
from pathlib import Path

import pytest


def version_tuple(value):
    return tuple(int(part) for part in value.split("."))


@pytest.mark.parametrize("host,sdk", [("1.3.19", "2.20.0"), ("1.4.5", "2.23.0")])
def test_manifest_includes_verified_host_and_sdk(host, sdk):
    path = Path(__file__).resolve().parents[1] / "_manifest.json"
    manifest = json.loads(path.read_text("utf-8"))
    for key, version in [("host_application", host), ("sdk", sdk)]:
        declared = manifest[key]
        minimum = version_tuple(declared["min_version"])
        maximum = version_tuple(declared["max_version"])
        assert minimum <= version_tuple(version) <= maximum
