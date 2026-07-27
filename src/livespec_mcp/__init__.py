"""livespec: code intelligence + Spec traceability, shipped as an MCP server.

The distribution, the console command and the GitHub repo are all ``livespec``.
The import package stays ``livespec_mcp`` — renaming it would churn every
module path and ``@spec`` link for no user-visible gain.
"""

from importlib.metadata import PackageNotFoundError, version

# ``livespec-mcp`` is the pre-rename distribution name, still tried so an
# environment that installed the old wheel keeps reporting a real version
# instead of the source fallback. The fallback silently masked this: after the
# rename `livespec --version` printed "0.0.0+source" from a correctly installed
# wheel, because the lookup name no longer matched anything.
for _dist in ("livespec", "livespec-mcp"):
    try:
        __version__ = version(_dist)
        break
    except PackageNotFoundError:
        continue
else:  # imported from source without an installed distribution
    __version__ = "0.0.0+source"
