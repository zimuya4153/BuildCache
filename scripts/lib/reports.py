"""The tables BuildCache publishes (release bodies and anything else generated).

The framework owns a small *mandatory* core: version, build hash and the artifact
itself. Everything else a row could show -- LeviLamina's ``server``/``client``
entry, the toolchain a build answers to, project specific flags -- is project
knowledge, so it is not baked in here. A plugin *registers* those columns through
the :class:`Table` manager::

    def register_release_columns(table):
        table.add("entry", title="entry", render=reports.code,
                  note="the build entry this artifact belongs to")

A column whose value is empty in every row of a table is not rendered at all,
which is what keeps a dependency's release body free of concepts that mean
nothing for it. Tools use the same manager, so what a release body shows and what
``cache.py list`` shows cannot drift apart.
"""

from __future__ import annotations

from . import semver, util


# --------------------------------------------------------------------- renderers
def code(value, row: dict | None = None) -> str:
    """Render a value as inline code."""
    return f"`{value}`" if value not in (None, "") else ""


def plain(value, row: dict | None = None) -> str:
    return "" if value in (None, "") else str(value)


def link(value, row: dict | None = None) -> str:
    """Render an asset name as a direct download link when the row has a url."""
    if value in (None, ""):
        return ""
    url = (row or {}).get("url")
    return f"[{value}]({url})" if url else str(value)


def listed(value, row: dict | None = None) -> str:
    """Render a list of flags as `a`, `b`."""
    if not value:
        return ""
    return ", ".join(f"`{item}`" for item in value)


#: Where a column sits in a table. Left to right is "what a reader looks up" to
#: "how it happens to have been built": the version, whatever the project needs
#: to say about it, the file to download -- the hash and the toolset trail.
ORDER_VERSION = 0
ORDER_ENTRY = 10
ORDER_ARTIFACT = 20
ORDER_DEFAULT = 50
ORDER_BUILD = 80

#: Columns every table has. The hash and the toolset share one order: they are
#: registered in the order they are meant to appear.
CORE = (
    {"key": "version", "title": "version", "render": code, "order": ORDER_VERSION},
    {"key": "asset", "title": "asset", "render": link, "order": ORDER_ARTIFACT},
    {"key": "buildhash", "title": "build hash", "render": code, "order": ORDER_BUILD},
    # A property of any Windows artifact, not of a project: which toolset it was
    # built for (a consumer can only select it with an at-least-as-new toolset).
    {"key": "toolset", "title": "toolset", "render": code, "order": ORDER_BUILD},
)


class Table:
    """The columns a generated table has; plugins extend it, tools reuse it."""

    def __init__(self, core=CORE):
        self._fields: list[dict] = [dict(field) for field in core]

    # ------------------------------------------------------------------ columns
    def add(
        self,
        key: str,
        *,
        title: str | None = None,
        render=None,
        note: str = "",
        order: int = ORDER_DEFAULT,
    ) -> "Table":
        """Register (or replace) a column. Registering twice replaces it.

        ``order`` places it among the others; the ``ORDER_*`` constants name the
        slots (a project's own columns default to just after the artifact).
        """
        self.remove(key)
        self._fields.append(
            {
                "key": key,
                "title": title or key,
                "render": render or plain,
                "note": note,
                "order": order,
            }
        )
        self._fields.sort(key=lambda field: field.get("order", ORDER_DEFAULT))
        return self

    def remove(self, key: str) -> "Table":
        """Drop a column by key (a no-op when it is not there)."""
        self._fields = [field for field in self._fields if field["key"] != key]
        return self

    def keys(self) -> list[str]:
        return [field["key"] for field in self._fields]

    def has(self, key: str) -> bool:
        return key in self.keys()

    # ------------------------------------------------------------------- render
    def visible(self, rows: list[dict]) -> list[dict]:
        """The columns that carry information for these rows."""
        return [
            field
            for field in self._fields
            if any(_cell(field, row) for row in rows)
        ]

    def render(self, rows: list[dict], title: str = "") -> list[str]:
        """Markdown table lines (header, separator, rows); empty when no rows."""
        if not rows:
            return []
        fields = self.visible(rows)
        lines: list[str] = []
        if title:
            lines += ["", f"{title} ({len(rows)}):", ""]
        else:
            lines.append("")
        lines.append("| " + " | ".join(field["title"] for field in fields) + " |")
        lines.append("|" + "|".join("---" for _ in fields) + "|")
        for row in rows:
            lines.append("| " + " | ".join(_cell(field, row) or "-" for field in fields) + " |")
        notes = [field["note"] for field in fields if field.get("note")]
        for note in notes:
            lines += ["", note]
        return lines


def _cell(field: dict, row: dict) -> str:
    value = row.get(field["key"])
    return field["render"](value, row)


def for_plugins(config: dict, ctx, plugins_list) -> Table:
    """The table a release body uses: the core plus every plugin's columns.

    Each project registers what its own packages need (and only that), which is
    how ``entry`` or a toolchain column ends up in a LeviLamina release body
    without the framework knowing what either of them means.
    """
    table = Table()
    for plugin in plugins_list:
        try:
            plugin.call("register_release_columns", table, ctx)
        except TypeError as error:
            util.warn(f"plugin {plugin.name}: register_release_columns failed: {error}")
    return table


def artifact_row(
    artifact: dict,
    *,
    version: str = "",
    entry_id: str = "",
    entry: dict | None = None,
    own: bool = True,
) -> dict:
    """One table row for an artifact, in the shape the columns read.

    ``own`` says whether the artifact belongs to the project's own package: a
    dependency is one artifact whichever entry of the project pulled it in, so the
    entry and the compiler of the *project* are left empty for it (and the columns
    disappear from its table).
    """
    entry = entry or {}
    return {
        "version": version or artifact.get("version") or "",
        "buildhash": artifact.get("buildhash") or "",
        "toolset": artifact.get("toolset") or "",
        "asset": artifact.get("asset") or "",
        "url": artifact.get("url") or "",
        "release": artifact.get("release") or "",
        "entry": entry_id if own else "",
        "compiled_with": (
            entry.get("compiled_with")
            or entry.get("upstream_toolchain")
            or entry.get("toolchain")
            or ""
        )
        if own
        else "",
        "aliases": sorted(
            item.get("id") or item.get("toolchain") or ""
            for item in artifact.get("aliases") or []
        ),
    }


def newest_first(rows: list[dict]) -> list[dict]:
    """Order rows the way a reader expects them: newest version on top.

    A version is not a string, so the sort has to go through the version parser
    (a plain sort ranks ``0.13.5`` above ``0.8.3``). Versions of one release
    shard do not repeat often -- 26.51.6 is built for both entries -- so the
    entries of a version keep a fixed order instead of being reversed with it.
    """
    ordered = sorted(
        rows,
        key=lambda item: (item.get("entry") or "", item.get("buildhash") or ""),
    )
    ordered.sort(
        key=lambda item: semver.key(str(item.get("version") or "")),
        reverse=True,
    )
    return ordered
