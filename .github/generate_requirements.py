import configparser
import logging
from typing import Any

_LOGGER = logging.getLogger(__name__)

try:
    import tomllib
except ModuleNotFoundError:
    tomllib = None


_SIMPLE_PIPFILE_ALLOWED_KEYS: set[str] = {
    "version",
    "git",
    "ref",
    "extras",
    "editable",
}


def _parse_simple_pipfile_inline(value: str) -> dict[str, Any] | None:
    """Best-effort parser for very simple Pipfile inline tables.

    This is intentionally conservative and only supports a small set of keys and
    trivial value forms. Anything more complex returns None instead of trying
    to be clever and possibly misparsing.

    The accepted format is:

        {key1 = value1, key2 = value2, ...}

    where:
      * keys are in ``_SIMPLE_PIPFILE_ALLOWED_KEYS``
      * values for ``version``, ``git``, and ``ref`` may be quoted strings
        (quotes are stripped) or bare tokens without whitespace
      * ``extras`` may be a bracketed list of quoted strings, e.g.
        ``extras = ["foo", "bar baz"]``
      * nested structures, unquoted strings with spaces, and unknown keys are
        rejected
    """
    stripped = value.strip()
    if not stripped.startswith("{") or not stripped.endswith("}"):
        return None

    inner = stripped[1:-1].strip()
    if not inner:
        return None

    result: dict[str, Any] = {}

    for part in inner.split(","):
        part = part.strip()
        if not part:
            continue

        if "=" not in part:
            return None

        key_raw, value_raw = part.split("=", 1)
        key = key_raw.strip()
        raw_value = value_raw.strip()

        if key not in _SIMPLE_PIPFILE_ALLOWED_KEYS:
            return None

        if key == "extras":
            if not (raw_value.startswith("[") and raw_value.endswith("]")):
                return None
            inner_list = raw_value[1:-1].strip()
            if not inner_list:
                result[key] = []
                continue
            extras: list[str] = []
            for item in inner_list.split(","):
                item = item.strip()
                if len(item) >= 2 and (
                    (item[0] == item[-1] == '"') or (item[0] == item[-1] == "'")
                ):
                    extras.append(item[1:-1])
                else:
                    return None
            result[key] = extras
            continue

        if raw_value.lower() == "true":
            parsed_value: Any = True
        elif raw_value.lower() == "false":
            parsed_value = False
        elif len(raw_value) >= 2 and (
            (raw_value[0] == raw_value[-1] == '"')
            or (raw_value[0] == raw_value[-1] == "'")
        ):
            parsed_value = raw_value[1:-1]
        elif any(ch.isspace() for ch in raw_value):
            return None
        else:
            parsed_value = raw_value

        result[key] = parsed_value

    return result or None


def _normalize_pipfile_dict(key: str, value: str) -> dict[str, Any] | None:
    """Convert a Pipfile TOML-style inline table string to a Python dict.

    This first tries TOML parsing via tomllib. If that fails or yields a
    non-mapping, we fall back to a very conservative parser that only accepts
    simple inline tables for CI requirements generation. More complex forms are
    rejected instead of being heuristically normalized.

    Supported syntax (when the stdlib ``tomllib`` module is unavailable) is
    intentionally limited, but it does include the most common inline table
    forms used in Pipfile entries, for example::

        {version = ">=1.2", extras = ["foo", "bar"]}

    The fallback parser accepts:

    * Double- or single-quoted string values for ``version``, ``git``, and
      ``ref`` (quotes are stripped).
    * ``extras`` values as a bracketed list of quoted strings, separated by
      commas and optional whitespace, e.g. ``extras = ["foo", "bar baz"]``.

    Any keys outside of ``_SIMPLE_PIPFILE_ALLOWED_KEYS`` or syntactically
    invalid inline tables will still be rejected. When the environment provides
    ``tomllib``, it is used first and supports the full TOML syntax.

    If parsing fails entirely, a warning is logged and the dependency is treated
    as an unpinned requirement (only the key is kept).
    """
    inline = value.strip()
    if not (inline.startswith("{") and inline.endswith("}")):
        return None

    if tomllib is not None:
        parsed = _parse_pipfile_dict_via_tomllib(key, inline)
        if parsed is not None:
            return parsed

    try:
        parsed = _parse_simple_pipfile_inline(inline)
    except Exception:
        parsed = None
    if parsed is None:
        _LOGGER.warning(
            "Skipping Pipfile inline metadata for %r because it could not be"
            " parsed: %r",
            key,
            inline,
        )
    return parsed


def _parse_pipfile_dict_via_tomllib(key: str, inline: str) -> dict[str, Any] | None:
    """Parse a Pipfile inline table using the stdlib TOML parser."""
    toml_doc = f"entry = {inline}\n"
    try:
        parsed = tomllib.loads(toml_doc)
    except Exception:
        _LOGGER.warning("Failed to parse Pipfile dict via TOML for %r: %r", key, inline)
        return None
    entry = parsed.get("entry")
    if isinstance(entry, dict):
        return entry
    _LOGGER.warning("Pipfile dict via TOML is not a mapping for %r: %r", key, inline)
    return None


def _parse_pipfile_value(key: str, value: str) -> str:
    """Convert a Pipfile package value to a pip requirement string.

    Always returns the complete requirement string including the package name.
    """
    value = value.strip()

    if not value.startswith("{"):
        if (value.startswith('"') and value.endswith('"')) or (
            value.startswith("'") and value.endswith("'")
        ):
            value = value[1:-1].strip()
        return f"{key}{value}" if value else key

    data = _normalize_pipfile_dict(key, value)
    if data is None:
        return key

    if git_url := data.get("git"):
        return _build_git_requirement(data, key, git_url)
    if version := data.get("version"):
        extras = data.get("extras")
        extras_spec = ""
        if isinstance(extras, (list, tuple)) and extras:
            extras_spec = "[" + ",".join(str(e) for e in extras) + "]"
        return f"{key}{extras_spec}{version}"

    return key


def _build_git_requirement(data: dict[str, Any], key: str, git_url: str) -> str:
    """Build a pip VCS requirement string for a git-based dependency."""
    extras = data.get("extras")
    extras_spec = ""
    if isinstance(extras, (list, tuple)) and extras:
        extras_spec = "[" + ",".join(str(e) for e in extras) + "]"
    ref_keys = ["ref", "tag", "branch"]
    present = [k for k in ref_keys if data.get(k)]
    if len(present) > 1:
        _LOGGER.warning(
            "Multiple git ref keys present for %r (%s); using %s",
            key,
            ", ".join(present),
            present[0],
        )
    rev = data.get(present[0]) if present else None
    if not isinstance(rev, str) or not rev.strip():
        rev = None
    url = f"git+{git_url}@{rev}" if rev else f"git+{git_url}"
    return f"{key}{extras_spec} @ {url}"


def main():
    parser = configparser.ConfigParser()
    parser.read("Pipfile")

    packages = "packages"
    with open("requirements.txt", "w") as f:
        for key in parser[packages]:
            value = parser[packages][key]
            req = _parse_pipfile_value(key, value)
            f.write(f"{req}\n")

    devpackages = "dev-packages"
    with open("requirements_tests.txt", "w") as f:
        for key in parser[devpackages]:
            value = parser[devpackages][key]
            req = _parse_pipfile_value(key, value)
            f.write(f"{req}\n")


if __name__ == "__main__":
    main()
