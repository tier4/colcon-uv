"""Install dependencies for UV packages."""

import argparse
import logging
import subprocess
import sys
from pathlib import Path
from typing import List, Optional

import tomli

logger = logging.getLogger("colcon.uv.dependencies")


class NotAUvPackageError(Exception):
    """Raised when a directory is not a UV package."""

    pass


class UvPackage:
    """Represents a UV package."""

    def __init__(self, path: Path, logger=None):
        """Initialize UV package."""
        self.path = path
        self.logger = logger or logging.getLogger(__name__)

        self.pyproject_file = path / "pyproject.toml"
        if not self.pyproject_file.exists():
            raise NotAUvPackageError(f"No pyproject.toml found in {path}")

        # Load pyproject.toml
        with open(self.pyproject_file, "rb") as f:
            self.pyproject_data = tomli.load(f)

        # Check if it's a UV package
        if (
            "tool" not in self.pyproject_data
            or "colcon-uv-ros" not in self.pyproject_data["tool"]
        ):
            raise NotAUvPackageError(
                f"No [tool.colcon-uv-ros] section found in {self.pyproject_file}"
            )

        # Get package name
        if (
            "project" in self.pyproject_data
            and "name" in self.pyproject_data["project"]
        ):
            self.name = self.pyproject_data["project"]["name"]
        else:
            self.name = path.name


def main():
    """Main entry point for UV dependency installation."""
    args = _parse_args()
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s:%(name)s: %(message)s",
    )

    for project in discover_packages(args.base_paths):
        logger.info(f"Installing dependencies for {project.path.name}...")
        install_dependencies(
            project, args.install_base, args.merge_install,
            dependency_groups=args.dependency_groups,
            extras=args.extras,
        )

    logger.info("Dependencies installed!")


def discover_packages(base_paths: List[Path]) -> List[UvPackage]:
    """Discover UV packages in the given base paths."""
    projects: List[UvPackage] = []

    potential_packages = []
    for path in base_paths:
        potential_packages += list(path.glob("*"))

    for path in potential_packages:
        if path.is_dir():
            try:
                project = UvPackage(path)
            except NotAUvPackageError:
                continue
            else:
                projects.append(project)

    if len(projects) == 0:
        base_paths_str = ", ".join([str(p) for p in base_paths])
        logger.error(
            f"No UV packages were found in the following paths: {base_paths_str}"
        )
        sys.exit(1)

    return projects


def _resolve_python_version(project: UvPackage) -> str:
    """Resolve the Python version to use for a project's virtual environment.

    Checks in order:
      1. .python-version file in the project directory
      2. requires-python field in pyproject.toml
      3. The interpreter running colcon (sys.executable)
    """
    # 1. .python-version (uv / pyenv convention)
    python_version_file = project.path / ".python-version"
    if python_version_file.exists():
        version = python_version_file.read_text().strip()
        if version:
            logger.info(f"Using Python version from .python-version: {version}")
            return version

    # 2. requires-python from pyproject.toml
    requires_python = project.pyproject_data.get("project", {}).get(
        "requires-python", ""
    )
    if requires_python:
        logger.info(f"Using requires-python from pyproject.toml: {requires_python}")
        return requires_python

    # 3. Fallback to the Python running colcon
    logger.info(f"Using colcon's Python: {sys.executable}")
    return sys.executable


def install_dependencies(
    project: UvPackage,
    install_base: Path,
    merge_install: bool,
    dependency_groups: Optional[List[str]] = None,
    extras: Optional[List[str]] = None,
) -> None:
    """Install dependencies for a UV package using UV."""
    # Handle both contexts:
    # 1. Direct install: install_base = /install, need to add package name
    # 2. Build task: install_base = /install/package_name, already included

    if not merge_install:
        # Check if install_base already ends with the package name
        if install_base.name != project.name:
            install_base /= project.name

    # Create the install directory first
    install_base.mkdir(parents=True, exist_ok=True)

    # Venv path - this should be /install/PACKAGE_NAME/venv/
    venv_path = install_base / "venv"

    # Determine the Python version for the virtual environment.
    # Priority:
    #   1. .python-version file in the project directory (uv convention)
    #   2. requires-python from pyproject.toml
    #   3. sys.executable (the Python running colcon / ROS)
    # Without an explicit version, uv defaults to the highest Python on the
    # system, which can break Boost.Python, ROS system packages, etc.
    python_version = _resolve_python_version(project)

    # --system-site-packages is needed because ROS 2 packages like rclpy are installed
    # system-wide (not available on PyPI) and our nodes need access to them
    if (venv_path / "bin" / "python").exists():
        logger.info(f"Reusing existing venv at {venv_path}")
    else:
        try:
            subprocess.run(
                [
                    "uv", "venv",
                    "--system-site-packages",
                    "--python", python_version,
                    str(venv_path),
                ],
                check=True,
                capture_output=True,
                text=True,
            )
        except subprocess.CalledProcessError as e:
            logger.error(f"Failed to create venv: {e.stderr}")
            raise

    # Install dependencies and the package itself to the target venv
    # Use --python to specify the target venv's python
    python_exe = venv_path / "bin" / "python"

    optional_deps = project.pyproject_data.get("project", {}).get(
        "optional-dependencies", {}
    )

    if optional_deps:
        if extras is None:
            # No --extras flag: check per-package default in
            # [tool.colcon-uv-ros].extras, then fall back to all extras
            default_extras = (
                project.pyproject_data.get("tool", {})
                .get("colcon-uv-ros", {})
                .get("extras", None)
            )
            if default_extras is not None:
                extra_names = [e for e in default_extras if e in optional_deps]
                unknown = set(default_extras) - set(optional_deps.keys())
                if unknown:
                    logger.warning(
                        f"Default extras not found in "
                        f"[project.optional-dependencies]: {', '.join(sorted(unknown))}"
                    )
            else:
                extra_names = list(optional_deps.keys())
        elif len(extras) == 0:
            # --extras with no args: install no extras
            extra_names = []
        else:
            # --extras codegen gui: install only the specified extras
            unknown_extras = set(extras) - set(optional_deps.keys())
            if unknown_extras:
                logger.warning(
                    f"Requested extras not found in pyproject.toml: "
                    f"{', '.join(sorted(unknown_extras))}. "
                    f"Available extras: {', '.join(sorted(optional_deps.keys()))}"
                )
            extra_names = [e for e in extras if e in optional_deps]

        if extra_names:
            extras_str = ",".join(extra_names)
            install_target = f"{project.path}[{extras_str}]"
            logger.info(f"Installing with optional dependencies: {extras_str}")
        else:
            install_target = str(project.path)
            logger.info("Installing without optional dependencies")
    else:
        install_target = str(project.path)

    # Build override arguments from [tool.uv].override-dependencies.
    # uv pip install does not read override-dependencies from pyproject.toml,
    # so we materialise them into a temporary requirements file and pass it
    # via --override.
    override_args: List[str] = []
    override_deps = (
        project.pyproject_data.get("tool", {})
        .get("uv", {})
        .get("override-dependencies", [])
    )
    override_file: Optional[Path] = None
    if override_deps:
        import tempfile
        override_file = Path(
            tempfile.mktemp(prefix="colcon_uv_override_", suffix=".txt")
        )
        override_file.write_text("\n".join(override_deps) + "\n")
        override_args = ["--override", str(override_file)]
        logger.info(f"Using override-dependencies: {override_deps}")

    try:
        subprocess.run(
            [
                "uv",
                "--no-progress",
                "pip",
                "install",
                "--python",
                str(python_exe),
                *override_args,
                "-e",
                install_target,
            ],
            check=True,
            stdout=sys.stdout,
            stderr=sys.stderr,
            text=True,
        )
    except subprocess.CalledProcessError as e:
        # UV writes its errors to stderr, pass them through to user
        if e.stderr:
            sys.stderr.write(e.stderr)
            sys.stderr.flush()

        # Log simply without the exception details
        logger.error(f"Failed to install dependencies for {install_target}")

        # Re-raise without the traceback by using sys.exit
        # This prevents colcon from printing the full Python traceback
        sys.exit(1)

    # Additionally, install dependency groups (PEP 735) if present
    available_groups = project.pyproject_data.get("dependency-groups", {})

    if available_groups:
        if dependency_groups is None:
            # No --dependency-groups flag: check per-package default in
            # [tool.colcon-uv-ros].dependency-groups, then fall back to all groups
            default_groups = (
                project.pyproject_data.get("tool", {})
                .get("colcon-uv-ros", {})
                .get("dependency-groups", None)
            )
            if default_groups is not None:
                group_names = [g for g in default_groups if g in available_groups]
                unknown = set(default_groups) - set(available_groups.keys())
                if unknown:
                    logger.warning(
                        f"Default dependency groups not found in "
                        f"[dependency-groups]: {', '.join(sorted(unknown))}"
                    )
            else:
                group_names = list(available_groups.keys())
        elif len(dependency_groups) == 0:
            # --dependency-groups with no args: install no groups
            group_names = []
        else:
            # --dependency-groups dev test: install only the specified groups
            unknown_groups = set(dependency_groups) - set(available_groups.keys())
            if unknown_groups:
                logger.warning(
                    f"Requested dependency groups not found in pyproject.toml: "
                    f"{', '.join(sorted(unknown_groups))}. "
                    f"Available groups: {', '.join(sorted(available_groups.keys()))}"
                )
            group_names = [g for g in dependency_groups if g in available_groups]
    else:
        group_names = []

    if group_names:
        logger.info(f"Installing dependency groups: {', '.join(group_names)}")

        cmd = ["uv", "--no-progress", "pip", "install", "--python", str(python_exe)]
        cmd.extend(override_args)
        for group in group_names:
            cmd.extend(["--group", group])
        cmd.append(".")

        try:
            subprocess.run(
                cmd, check=True, stdout=sys.stdout, stderr=sys.stderr, text=True,
                cwd=str(project.path),
            )
        except subprocess.CalledProcessError as e:
            # UV writes its errors to stderr, pass them through to user
            if e.stderr:
                sys.stderr.write(e.stderr)
                sys.stderr.flush()

            # Log simply without the exception details
            logger.error(f"Failed to install dependency groups for {project.name}")

            # Re-raise without the traceback by using sys.exit
            # This prevents colcon from printing the full Python traceback
            sys.exit(1)

    # Clean up the temporary override file
    if override_file and override_file.exists():
        override_file.unlink()


def install_dependencies_from_descriptor(
    pkg_descriptor,
    install_base: Path,
    merge_install: bool,
    dependency_groups: Optional[List[str]] = None,
    extras: Optional[List[str]] = None,
):
    """Install dependencies from a PackageDescriptor object.

    This is a convenience function for use by colcon build tasks.
    """
    try:
        uv_package = UvPackage(pkg_descriptor.path)
        install_dependencies(
            uv_package, install_base, merge_install,
            dependency_groups=dependency_groups,
            extras=extras,
        )
    except NotAUvPackageError as e:
        # Skip packages that aren't UV packages
        logger.debug(f"Skipping non-UV package {pkg_descriptor.name}: {e}")
        return


def _parse_args() -> argparse.Namespace:
    """Parse command line arguments."""
    parser = argparse.ArgumentParser(
        description="Searches for UV packages and installs their dependencies "
        "to a configurable install base"
    )

    parser.add_argument(
        "--base-paths",
        nargs="+",
        type=Path,
        default=[Path.cwd()],
        help="The paths to start looking for UV projects in. Defaults to the "
        "current directory.",
    )

    parser.add_argument(
        "--install-base",
        type=Path,
        default=Path("install"),
        help="The base path for all install prefixes (default: install)",
    )

    parser.add_argument(
        "--merge-install",
        action="store_true",
        help="Merge all install prefixes into a single location",
    )

    parser.add_argument(
        "--dependency-groups",
        nargs="*",
        metavar="GROUP",
        type=str,
        default=None,
        help="Specify which dependency groups to install. "
        "If not provided, all groups are installed. "
        "Pass with no arguments to install no groups.",
    )

    parser.add_argument(
        "--extras",
        nargs="*",
        metavar="EXTRA",
        type=str,
        default=None,
        help="Specify which optional dependency extras to install. "
        "If not provided, all extras are installed "
        "(or per-package default from [tool.colcon-uv-ros].extras). "
        "Pass with no arguments to install no extras.",
    )

    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="If provided, debug logs will be printed",
    )

    return parser.parse_args()


if __name__ == "__main__":
    main()
