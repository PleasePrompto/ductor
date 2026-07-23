"""Shared Gemini CLI utilities (used by provider and cron/webhook execution)."""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import tempfile
from pathlib import Path
from shutil import which

from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS
from ductor_bot.infra.platform import is_windows

logger = logging.getLogger(__name__)


def find_gemini_cli() -> str:
    """Find the ``gemini`` CLI on PATH.

    Raises:
        FileNotFoundError: If the CLI is not found.
    """
    path = which("gemini")
    if path:
        return path
    fallback_path = _find_gemini_fallback(Path.home())
    if fallback_path:
        return fallback_path
    msg = "gemini CLI not found on PATH. Install via: npm install -g @google/gemini-cli"
    raise FileNotFoundError(msg)


def find_gemini_cli_js() -> str | None:
    """Find the Gemini CLI's ``index.js`` via ``npm root -g``.

    Returns the absolute path to ``index.js``, or ``None`` if not found.
    """
    npm_path = which("npm")
    if npm_path:
        try:
            root = subprocess.check_output(
                [npm_path, "root", "-g"],
                text=True,
                encoding="utf-8",
                stderr=subprocess.DEVNULL,
                creationflags=_CREATION_FLAGS,
            ).strip()
            candidate = _gemini_index_from_node_modules_root(Path(root))
            if candidate.is_file():
                return str(candidate)
        except (subprocess.SubprocessError, OSError):
            pass

    # Fallback for service environments where npm isn't on PATH but gemini is
    # installed under an NVM node version.
    try:
        cli_path = Path(find_gemini_cli())
        cli_path = cli_path.resolve()
    except FileNotFoundError:
        return None
    except OSError:
        pass

    for candidate in _gemini_index_candidates_from_cli_path(cli_path):
        if candidate.is_file():
            return str(candidate)
    return None


def discover_gemini_models() -> frozenset[str]:
    """Discover Gemini models from the installed Gemini CLI config files.

    Two layouts are supported:

    1. Older releases ship ``gemini-cli-core/dist/src/config/models.js`` as
       a separate ESM module that re-exports ``VALID_GEMINI_MODELS``.
    2. ``@google/gemini-cli`` ≥ 0.37 ships everything as a pre-bundled
       ``bundle/*.js`` set; the model list lives inside one of the hashed
       chunks as ``VALID_GEMINI_MODELS = new Set([...])`` referencing
       ``const`` identifiers defined in the same file.
    """
    for models_js in _gemini_models_js_candidates():
        models = _discover_models_from_models_js(models_js)
        if models:
            return models
    for bundle_js in _gemini_bundle_candidates():
        models = _discover_models_from_bundle(bundle_js)
        if models:
            return models
    return frozenset()


def trust_workspace(working_dir: Path) -> None:
    """Add *working_dir* to ``~/.gemini/trustedFolders.json``."""
    gemini_home = Path.home() / ".gemini"
    trust_file = gemini_home / "trustedFolders.json"
    workspace_path = str(working_dir)

    if os.name == "nt":
        workspace_path = workspace_path.replace("/", "\\")

    try:
        data: dict[str, str] = {}
        if trust_file.is_file():
            try:
                data = json.loads(trust_file.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                logger.warning("Corrupt Gemini trust file, starting fresh")

        if workspace_path not in data:
            from ductor_bot.infra.json_store import atomic_json_save

            data[workspace_path] = "TRUST_FOLDER"
            gemini_home.mkdir(parents=True, exist_ok=True)
            atomic_json_save(trust_file, data)
            logger.info("Trusted workspace in Gemini CLI: %s", workspace_path)
    except OSError:
        logger.warning("Failed to update Gemini trusted folders", exc_info=True)


def create_system_prompt_file(
    system_prompt: str,
    append_prompt: str = "",
    *,
    directory: str | None = None,
    prefix: str = "gemini_system_",
) -> str:
    """Write system prompt to a temp file, return path. Caller must clean up.

    When *directory* is set the temp file is placed there instead of the
    system default (useful for Docker mounts like ``~/.ductor/tmp``).  The
    file keeps the default ``0600`` permissions so prompt content stays private.
    """
    content = system_prompt
    if append_prompt:
        content = f"{content}\n\n{append_prompt}"
    with tempfile.NamedTemporaryFile(
        mode="w",
        suffix=".md",
        prefix=prefix,
        delete=False,
        encoding="utf-8",
        dir=directory,
    ) as tf:
        tf.write(content)
        return tf.name


def _gemini_models_js_candidates() -> tuple[Path, ...]:
    candidates: list[Path] = []

    npm_root = _npm_global_root()
    if npm_root is not None:
        candidates.append(_gemini_models_js_from_node_modules_root(npm_root))

    try:
        cli_path = Path(find_gemini_cli()).resolve()
    except (FileNotFoundError, OSError):
        cli_path = None

    if cli_path is not None:
        candidates.extend(_gemini_models_js_candidates_from_cli_path(cli_path))

    return tuple(dict.fromkeys(candidates))


def _npm_global_root() -> Path | None:
    npm_path = which("npm")
    if not npm_path:
        return None
    try:
        root = subprocess.check_output(
            [npm_path, "root", "-g"],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
            creationflags=_CREATION_FLAGS,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return None
    return Path(root) if root else None


def _gemini_models_js_from_node_modules_root(node_modules_root: Path) -> Path:
    return (
        node_modules_root
        / "@google"
        / "gemini-cli"
        / "node_modules"
        / "@google"
        / "gemini-cli-core"
        / "dist"
        / "src"
        / "config"
        / "models.js"
    )


def _gemini_models_js_candidates_from_cli_path(cli_path: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []

    # When cli_path is a resolved symlink (e.g.
    # .../gemini-cli/dist/index.js), walk up to the gemini-cli package root
    # and look for models.js in its node_modules.
    gemini_cli_root = _find_gemini_cli_package_root(cli_path)
    if gemini_cli_root is not None:
        candidates.append(
            gemini_cli_root
            / "node_modules"
            / "@google"
            / "gemini-cli-core"
            / "dist"
            / "src"
            / "config"
            / "models.js"
        )

    # NVM layout: .../bin/gemini -> two parents up is the node version dir.
    # Only useful when cli_path is the unresolved bin-stub, not the resolved
    # dist/index.js path.
    node_version_dir = cli_path.parent.parent
    candidates.append(
        node_version_dir
        / "lib"
        / "node_modules"
        / "@google"
        / "gemini-cli"
        / "node_modules"
        / "@google"
        / "gemini-cli-core"
        / "dist"
        / "src"
        / "config"
        / "models.js"
    )

    # npm global Windows/flat layout
    candidates.append(
        cli_path.parent
        / "node_modules"
        / "@google"
        / "gemini-cli"
        / "node_modules"
        / "@google"
        / "gemini-cli-core"
        / "dist"
        / "src"
        / "config"
        / "models.js"
    )

    return tuple(dict.fromkeys(candidates))


def _find_gemini_cli_package_root(path: Path) -> Path | None:
    """Walk up from *path* to find the ``@google/gemini-cli`` package root.

    Returns the directory that contains ``package.json`` with the
    ``@google/gemini-cli`` package name **and** a ``node_modules`` subdirectory
    (to skip ``dist/package.json`` copies that lack the actual dependency tree).
    """
    current = path if path.is_dir() else path.parent
    for _ in range(10):  # safety limit
        pkg = current / "package.json"
        if pkg.is_file() and (current / "node_modules").is_dir():
            try:
                data = json.loads(pkg.read_text(encoding="utf-8"))
                if isinstance(data, dict) and data.get("name") == "@google/gemini-cli":
                    return current
            except (OSError, json.JSONDecodeError):
                pass
        parent = current.parent
        if parent == current:
            break
        current = parent
    return None


def _discover_models_from_models_js(models_js: Path) -> frozenset[str]:
    if not models_js.is_file():
        return frozenset()

    models = _discover_models_via_node(models_js)
    if models:
        return models

    found = _extract_models_from_text(models_js.read_text(encoding="utf-8", errors="replace"))
    found.update(_extract_models_from_source_map(models_js))
    return frozenset(sorted(found))


def _discover_models_via_node(models_js: Path) -> frozenset[str]:
    node_bin = _find_node_binary()
    if node_bin is None:
        return frozenset()

    script = (
        "import { pathToFileURL } from 'node:url';"
        "const modelsPath = process.argv[1];"
        "const mod = await import(pathToFileURL(modelsPath).href);"
        "const valid = mod.VALID_GEMINI_MODELS;"
        "const out = [];"
        "if (valid && typeof valid[Symbol.iterator] === 'function') {"
        "for (const model of valid) {"
        "if (typeof model !== 'string') continue;"
        "out.push(model);"
        "}"
        "}"
        "console.log(JSON.stringify(out));"
    )
    try:
        output = subprocess.check_output(
            [node_bin, "--input-type=module", "-e", script, str(models_js)],
            text=True,
            encoding="utf-8",
            stderr=subprocess.DEVNULL,
            creationflags=_CREATION_FLAGS,
        ).strip()
    except (subprocess.SubprocessError, OSError):
        return frozenset()

    if not output:
        return frozenset()
    try:
        raw_models = json.loads(output)
    except json.JSONDecodeError:
        return frozenset()
    if not isinstance(raw_models, list):
        return frozenset()

    values = {item for item in raw_models if isinstance(item, str)}
    return frozenset(sorted(values))


def _find_node_binary() -> str | None:
    node = which("node")
    if node:
        return node
    try:
        cli = Path(find_gemini_cli()).resolve()
    except (FileNotFoundError, OSError):
        return None

    names = ("node.exe", "node") if is_windows() else ("node",)
    for name in names:
        candidate = cli.parent / name
        if candidate.is_file():
            return str(candidate)
    return None


def _extract_models_from_text(content: str) -> set[str]:
    pattern = re.compile(r"""['"]((?:auto-)?gemini-[\w.\-]+)['"]""")
    return set(pattern.findall(content))


# Matches `VALID_GEMINI_MODELS = new Set([...])`, tolerating an optional
# `/* @__PURE__ */` annotation inserted by esbuild.
_VALID_GEMINI_SET_RE = re.compile(
    r"VALID_GEMINI_MODELS\s*=\s*(?:/\*[^*]*\*/\s*)?new\s+Set\s*\(\s*\[(.*?)\]\s*\)",
    re.DOTALL,
)
_UPPER_IDENT_RE = re.compile(r"\b([A-Z][A-Z0-9_]*)\b")


def _extract_models_from_valid_set(content: str) -> set[str]:
    """Resolve identifiers listed inside ``VALID_GEMINI_MODELS = new Set([...])``.

    Each identifier is looked up in *content* via
    ``IDENT = "gemini-..."`` (with or without ``const``/``var``).
    Identifiers that cannot be resolved are silently skipped.
    """
    set_block = _VALID_GEMINI_SET_RE.search(content)
    if not set_block:
        return set()
    idents = _UPPER_IDENT_RE.findall(set_block.group(1))
    if not idents:
        return set()

    found: set[str] = set()
    for ident in dict.fromkeys(idents):
        ident_re = re.compile(
            r"\b" + re.escape(ident) + r"""\s*=\s*['"]((?:auto-)?gemini-[\w.\-]+)['"]"""
        )
        match = ident_re.search(content)
        if match:
            found.add(match.group(1))
    return found


def _gemini_bundle_candidates() -> tuple[Path, ...]:
    """Return ``bundle/*.js`` files of an installed ``@google/gemini-cli``.

    Newer releases ship the model list inside one of the hashed chunks
    instead of a stable ``models.js`` location.
    """
    candidates: list[Path] = []
    for bundle_root in _gemini_bundle_roots():
        try:
            candidates.extend(p for p in sorted(bundle_root.glob("*.js")) if p.is_file())
        except OSError:
            continue

    return tuple(dict.fromkeys(candidates))


def _gemini_bundle_roots() -> list[Path]:
    roots: list[Path] = []

    npm_root = _npm_global_root()
    if npm_root is not None:
        roots.append(npm_root / "@google" / "gemini-cli" / "bundle")

    try:
        cli_path = Path(find_gemini_cli()).resolve()
    except (FileNotFoundError, OSError):
        cli_path = None

    if cli_path is not None:
        pkg_root = _find_gemini_cli_package_root(cli_path)
        if pkg_root is not None:
            roots.append(pkg_root / "bundle")

        node_version_dir = cli_path.parent.parent
        roots.append(
            node_version_dir / "lib" / "node_modules" / "@google" / "gemini-cli" / "bundle"
        )
        roots.append(cli_path.parent / "node_modules" / "@google" / "gemini-cli" / "bundle")

    return list(dict.fromkeys(roots))


def _discover_models_from_bundle(bundle_js: Path) -> frozenset[str]:
    try:
        content = bundle_js.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return frozenset()
    if "VALID_GEMINI_MODELS" not in content:
        return frozenset()
    return frozenset(sorted(_extract_models_from_valid_set(content)))


def _extract_models_from_source_map(models_js: Path) -> set[str]:
    source_map = models_js.with_suffix(".js.map")
    if not source_map.is_file():
        return set()

    try:
        data = json.loads(source_map.read_text(encoding="utf-8", errors="replace"))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()

    if not isinstance(data, dict):
        return set()
    sources = data.get("sources")
    sources_content = data.get("sourcesContent")
    if not isinstance(sources, list) or not isinstance(sources_content, list):
        return set()

    found: set[str] = set()
    for source_name, content in zip(sources, sources_content, strict=False):
        if not isinstance(source_name, str) or not source_name.endswith("models.ts"):
            continue
        if isinstance(content, str):
            found.update(_extract_models_from_text(content))
    return found


def _find_gemini_fallback(home: Path) -> str | None:
    for bin_dir in _iter_gemini_bin_dirs(home):
        for name in _gemini_exec_names():
            candidate = bin_dir / name
            if candidate.is_file():
                return str(candidate)
    return None


def _iter_gemini_bin_dirs(home: Path) -> list[Path]:
    candidates: list[Path] = []
    if is_windows():
        appdata = os.environ.get("APPDATA")
        if appdata:
            candidates.append(Path(appdata) / "npm")
        candidates.append(home / "AppData" / "Roaming" / "npm")
        nvm_symlink = os.environ.get("NVM_SYMLINK")
        if nvm_symlink:
            candidates.append(Path(nvm_symlink))

    candidates.extend(_iter_nvm_bin_dirs(home))
    return list(dict.fromkeys(candidates))


def _iter_nvm_bin_dirs(home: Path) -> list[Path]:
    versions_dir = home / ".nvm" / "versions" / "node"
    if not versions_dir.is_dir():
        return []
    return [
        version_dir / "bin"
        for version_dir in sorted(versions_dir.iterdir(), reverse=True)
        if (version_dir / "bin").is_dir()
    ]


def _gemini_exec_names() -> tuple[str, ...]:
    if is_windows():
        return ("gemini.cmd", "gemini.exe", "gemini")
    return ("gemini",)


def _gemini_index_from_node_modules_root(node_modules_root: Path) -> Path:
    return node_modules_root / "@google" / "gemini-cli" / "dist" / "index.js"


def _gemini_index_candidates_from_cli_path(cli_path: Path) -> tuple[Path, ...]:
    candidates: list[Path] = []

    # When cli_path resolved to .../gemini-cli/dist/index.js, the package
    # root already contains dist/index.js.
    gemini_cli_root = _find_gemini_cli_package_root(cli_path)
    if gemini_cli_root is not None:
        candidates.append(gemini_cli_root / "dist" / "index.js")

    # NVM layout: bin/gemini -> two parents up is the node version dir.
    node_version_dir = cli_path.parent.parent
    candidates.append(
        node_version_dir / "lib" / "node_modules" / "@google" / "gemini-cli" / "dist" / "index.js"
    )
    # npm global Windows/flat layout
    candidates.append(
        cli_path.parent / "node_modules" / "@google" / "gemini-cli" / "dist" / "index.js"
    )

    return tuple(dict.fromkeys(candidates))
