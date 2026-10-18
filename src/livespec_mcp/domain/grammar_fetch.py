"""Fetch tree-sitter grammars over Python's TLS stack when the pack's own can't.

`tree-sitter-language-pack` 1.6.x downloads its parser bundle with a native
(Rust) client: `ureq` + `rustls` with the Mozilla roots compiled into the
wheel. It honours `HTTPS_PROXY`, but NOT `SSL_CERT_FILE`, `REQUESTS_CA_BUNDLE`
or the OS trust store. Behind a TLS-intercepting proxy (corporate MITM, agent
sandboxes) every download therefore fails with
`invalid peer certificate: UnknownIssuer`, even on a machine where `curl`,
`pip` and Python itself work fine — and every non-Python language indexes to
zero symbols.

This module does the same download with `urllib`, which uses OpenSSL: it
honours `SSL_CERT_FILE` natively, and we pass the first CA bundle named by
`LIVESPEC_CA_BUNDLE` / `SSL_CERT_FILE` / `REQUESTS_CA_BUNDLE` /
`CURL_CA_BUNDLE` explicitly. It reads the pack's own manifest (`parsers.json`
for the installed pack version), downloads this platform's bundle, checks its
sha256 against the manifest, and unpacks only the grammars asked for into the
pack's `cache_dir()` — the exact place the pack looks first, so the pack then
loads them with no network at all.

The bundle is a `.tar.zst`. Decompression uses, in order: `compression.zstd`
(Python 3.14+), the `zstandard` package, the `zstd` CLI. With none of them the
fetch fails with a hint saying so.

Failures are returned as exceptions carrying a message for the user; nothing
here imports the MCP surface.
"""

from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import ssl
import subprocess
import tarfile
import tempfile
import urllib.request
from pathlib import Path
from typing import Any

RELEASE_URL = "https://github.com/kreuzberg-dev/tree-sitter-language-pack/releases/download/v{version}/parsers.json"

# Env vars naming a CA bundle, in the order we try them. LIVESPEC_CA_BUNDLE
# first so a user can point livespec somewhere without touching the others.
CA_BUNDLE_ENV_VARS: tuple[str, ...] = (
    "LIVESPEC_CA_BUNDLE",
    "SSL_CERT_FILE",
    "REQUESTS_CA_BUNDLE",
    "CURL_CA_BUNDLE",
)

# Set to 0 / false / off to stop livespec from fetching grammars itself.
FALLBACK_ENV = "LIVESPEC_GRAMMAR_FALLBACK"

_LIB_RE = re.compile(r"^(?:lib)?tree_sitter_(?P<lang>[A-Za-z0-9_]+)\.(?:so|dylib|dll)$")
_TIMEOUT_S = 60


class GrammarFetchError(RuntimeError):
    """The Python-side fetch failed too; the message says why."""


def fallback_enabled() -> bool:
    return os.environ.get(FALLBACK_ENV, "1").strip().lower() not in ("0", "false", "off", "no")


def ca_bundle_from_env() -> tuple[str, str] | None:
    """(env var, path) of the first CA bundle env var that names a real file."""
    for var in CA_BUNDLE_ENV_VARS:
        val = os.environ.get(var, "").strip()
        if val and Path(val).is_file():
            return var, val
    return None


def _ssl_context() -> ssl.SSLContext:
    found = ca_bundle_from_env()
    if found:
        return ssl.create_default_context(cafile=found[1])
    return ssl.create_default_context()


def pack_version() -> str | None:
    try:
        from importlib.metadata import version

        return version("tree-sitter-language-pack")
    except Exception:
        return None


def platform_key() -> str | None:
    """The manifest's platform key for this machine (`linux-x86_64`, ...)."""
    system = platform.system().lower()
    machine = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(machine, machine)
    if system == "linux":
        return f"linux-{arch}"
    if system == "darwin":
        return "macos-arm64" if arch == "aarch64" else f"macos-{arch}"
    if system == "windows":
        return f"windows-{arch}"
    return None


def _open(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "livespec-grammar-fetch"})
    return urllib.request.urlopen(req, timeout=_TIMEOUT_S, context=_ssl_context())


_NO_ZSTD = (
    "no zstd decoder available to unpack the grammar bundle: install the "
    "`zstandard` package (`uv pip install zstandard` / `pip install "
    "zstandard`) or the `zstd` CLI, or use Python 3.14+"
)


def _zstd_backend() -> str | None:
    """Which decoder `_zstd_decompress_to` will use, or None. Checked BEFORE
    the 19 MB download, so a machine that cannot unpack never fetches."""
    try:  # Python 3.14+
        from compression import zstd  # type: ignore[import-not-found]  # noqa: F401

        return "stdlib"
    except ImportError:
        pass
    try:
        import zstandard  # type: ignore[import-not-found]  # noqa: F401

        return "zstandard"
    except ImportError:
        pass
    if shutil.which("zstd") or shutil.which("unzstd"):
        return "cli"
    return None


def _zstd_decompress_to(src: Path, dst: Path) -> None:
    """Decompress a .zst file with whatever decoder this machine has."""
    backend = _zstd_backend()
    if backend == "stdlib":
        from compression import zstd as _zstd  # type: ignore[import-not-found]

        with _zstd.open(src, "rb") as fin, dst.open("wb") as fout:
            shutil.copyfileobj(fin, fout)
    elif backend == "zstandard":
        import zstandard  # type: ignore[import-not-found]

        with src.open("rb") as fin, dst.open("wb") as fout:
            zstandard.ZstdDecompressor().copy_stream(fin, fout)
    elif backend == "cli":
        exe = shutil.which("zstd") or shutil.which("unzstd")
        with dst.open("wb") as fout:
            subprocess.run([str(exe), "-dc", str(src)], stdout=fout, check=True)
    else:
        raise GrammarFetchError(_NO_ZSTD)


def fetch_grammars(languages: list[str], cache_dir: str | os.PathLike[str]) -> dict[str, Any]:
    """Download the pack's bundle via urllib and unpack `languages` into `cache_dir`.

    Raises `GrammarFetchError` with a human-readable reason on any failure.
    Returns {"installed": [...], "missing_from_bundle": [...], "cache_dir",
    "ca_bundle": env var used or None, "source": "python"}.
    """
    if _zstd_backend() is None:
        raise GrammarFetchError(_NO_ZSTD)
    version = pack_version()
    if not version:
        raise GrammarFetchError("cannot tell which tree-sitter-language-pack version is installed")
    key = platform_key()
    if not key:
        raise GrammarFetchError(f"unsupported platform {platform.system()}/{platform.machine()}")
    manifest_url = RELEASE_URL.format(version=version)
    try:
        with _open(manifest_url) as resp:
            manifest = json.loads(resp.read().decode("utf-8"))
    except GrammarFetchError:
        raise
    except Exception as e:
        raise GrammarFetchError(f"could not fetch {manifest_url}: {e}") from e
    bundle = (manifest.get("platforms") or {}).get(key)
    if not bundle or not bundle.get("url"):
        raise GrammarFetchError(f"the {version} manifest has no bundle for platform {key}")

    dest = Path(cache_dir)
    dest.mkdir(parents=True, exist_ok=True)
    wanted = set(languages)
    installed: list[str] = []
    with tempfile.TemporaryDirectory(prefix="livespec-grammars-") as tmp:
        tmp_dir = Path(tmp)
        archive = tmp_dir / "bundle.tar.zst"
        digest = hashlib.sha256()
        try:
            with _open(bundle["url"]) as resp, archive.open("wb") as fout:
                while chunk := resp.read(1 << 20):
                    digest.update(chunk)
                    fout.write(chunk)
        except Exception as e:
            raise GrammarFetchError(f"could not download {bundle['url']}: {e}") from e
        expected = (bundle.get("sha256") or "").lower()
        if expected and digest.hexdigest() != expected:
            raise GrammarFetchError(
                f"checksum mismatch for {bundle['url']} (expected {expected}, "
                f"got {digest.hexdigest()}); refusing to install it"
            )
        tar_path = tmp_dir / "bundle.tar"
        try:
            _zstd_decompress_to(archive, tar_path)
        except GrammarFetchError:
            raise
        except Exception as e:
            raise GrammarFetchError(f"could not decompress the grammar bundle: {e}") from e
        try:
            with tarfile.open(tar_path, mode="r:") as tf:
                for member in tf:
                    if not member.isfile():
                        continue
                    m = _LIB_RE.match(Path(member.name).name)
                    if not m or m.group("lang") not in wanted:
                        continue
                    src = tf.extractfile(member)
                    if src is None:
                        continue
                    final = dest / Path(member.name).name  # basename only: no path traversal
                    part = final.with_name(final.name + ".part")
                    with part.open("wb") as fout:
                        shutil.copyfileobj(src, fout)
                    os.replace(part, final)
                    installed.append(m.group("lang"))
        except (tarfile.TarError, OSError) as e:
            raise GrammarFetchError(f"could not unpack the grammar bundle: {e}") from e
    found = ca_bundle_from_env()
    return {
        "installed": sorted(set(installed)),
        "missing_from_bundle": sorted(wanted - set(installed)),
        "cache_dir": str(dest),
        "ca_bundle": found[0] if found else None,
        "source": "python",
    }
