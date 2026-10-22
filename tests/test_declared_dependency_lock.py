"""The environment used by CI must satisfy declared runtime and development requirements."""

from pathlib import Path

try:
    import tomllib
except ModuleNotFoundError:
    import tomli as tomllib

from packaging.markers import default_environment
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name

ROOT = Path(__file__).resolve().parents[1]
LOCKS = ["requirements.lock"]
EXTRAS = ["dev"]


def test_locked_environment_satisfies_declared_requirements():
    metadata = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    declared = list(metadata["project"].get("dependencies", []))
    declared.extend(metadata["build-system"]["requires"])
    for extra in EXTRAS:
        declared.extend(metadata["project"]["optional-dependencies"][extra])
    for filename in LOCKS:
        locked = [
            Requirement(line)
            for line in (ROOT / filename).read_text(encoding="utf-8").splitlines()
            if line and not line.startswith(("#", " ", "-"))
        ]
        for version in ("3.10", "3.11", "3.12"):
            for platform in ("win32", "linux"):
                environment = {
                    **default_environment(),
                    "python_version": version,
                    "python_full_version": version + ".0",
                    "sys_platform": platform,
                }
                active = {
                    canonicalize_name(item.name): item
                    for item in locked
                    if item.marker is None or item.marker.evaluate(environment)
                }
                for text in declared:
                    requirement = Requirement(text)
                    if requirement.url or (
                        requirement.marker and not requirement.marker.evaluate(environment)
                    ):
                        continue
                    name = canonicalize_name(requirement.name)
                    assert name in active, f"{filename}: missing {name} on {version}/{platform}"
                    pins = [pin.version for pin in active[name].specifier if pin.operator == "=="]
                    assert len(pins) == 1, f"{filename}: {name} must have one exact version"
                    assert requirement.specifier.contains(pins[0]), (
                        f"{filename}: {name}=={pins[0]} does not satisfy {requirement.specifier}"
                    )
