"""Guard against __version__ / pyproject.toml drift (issue #12).

`statecharts.__version__` must equal the version pip installs (the pyproject.toml
value). This test fails if the two ever diverge again.
"""
import os
import re

import statecharts

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_PYPROJECT = os.path.join(_ROOT, "pyproject.toml")


def _pyproject_version() -> str:
    """Read the [project] version. Uses stdlib tomllib on 3.11+, else a small regex
    (the project targets >=3.10 with zero third-party deps)."""
    try:
        import tomllib  # Python 3.11+
        with open(_PYPROJECT, "rb") as f:
            return tomllib.load(f)["project"]["version"]
    except ModuleNotFoundError:
        with open(_PYPROJECT, "r", encoding="utf-8") as f:
            text = f.read()
        # first `version = "..."` after the [project] header
        section = text.split("[project]", 1)[1]
        m = re.search(r'^\s*version\s*=\s*"([^"]+)"', section, re.MULTILINE)
        assert m, "could not find version in [project] table of pyproject.toml"
        return m.group(1)


def test_version_matches_pyproject():
    assert statecharts.__version__ == _pyproject_version(), (
        f"__version__={statecharts.__version__!r} != "
        f"pyproject {_pyproject_version()!r}"
    )
