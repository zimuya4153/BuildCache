"""Temporary: show what one build result recorded."""

from __future__ import annotations

import json
import sys
from pathlib import Path

for path in sorted(Path("results").glob("build-result-*/*.json")):
    data = json.loads(path.read_text(encoding="utf-8"))
    print(
        f"== {path.name}: package={data['package']} entry={data['entry']} "
        f"toolchain={data['toolchain']} upstream={data['upstream_toolchain']} "
        f"xmake={data['xmake_version']} toolset={data['vs_toolset']} "
        f"profiles={data['alias_profiles']} artifacts={len(data['artifacts'])}"
    )
    without = 0
    for artifact in data["artifacts"]:
        aliases = artifact.get("aliases") or []
        if not aliases:
            without += 1
        described = " ".join(f"{item['id']}->{item['buildhash'][:8]}" for item in aliases)
        print(
            f"   {artifact['upstream_package']:24s} {artifact['version']:20s} "
            f"{artifact['buildhash'][:10]} {described or '(toolchain independent)'}"
        )
    print(f"   without aliases: {without}")
