from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Iterable, Optional


SCRIPT_NAME = "LiveSyncRemoteScript"
DEFAULT_CONVEX_VERSION = "0.7.0"
SIDECAR_VENV_NAME = ".sidecar-venv"


def repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


def source_dir() -> Path:
    return repo_root() / "ableton" / SCRIPT_NAME


def default_user_library_root() -> Path:
    return Path.home() / "Music" / "Ableton" / "User Library" / "Remote Scripts"


def detect_live_apps() -> Iterable[Path]:
    applications = Path("/Applications")
    if not applications.exists():
        return []
    return sorted(applications.glob("Ableton Live*.app"))


def resolve_bundle_root(bundle_path: Optional[str]) -> Path:
    if bundle_path:
        app_path = Path(bundle_path).expanduser().resolve()
    else:
        apps = list(detect_live_apps())
        if not apps:
            raise SystemExit("No Ableton Live app bundle found under /Applications.")
        if len(apps) > 1:
            choices = "\n".join("  - %s" % app for app in apps)
            raise SystemExit(
                "Multiple Ableton Live app bundles found. Re-run with --bundle-path.\n%s" % choices
            )
        app_path = apps[0]

    root = app_path / "Contents" / "App-Resources" / "MIDI Remote Scripts"
    if not root.exists():
        raise SystemExit("Bundle target does not exist: %s" % root)
    return root


def install(source: Path, target_root: Path, force: bool, dry_run: bool) -> Path:
    target = target_root / SCRIPT_NAME
    if target.exists():
        if not force:
            raise SystemExit(
                "Target already exists: %s\nRe-run with --force to replace it." % target
            )
        if dry_run:
            print("Would remove existing target: %s" % target)
        else:
            shutil.rmtree(target)

    if dry_run:
        print("Would create target root: %s" % target_root)
        print("Would copy %s -> %s" % (source, target))
        return target

    target_root.mkdir(parents=True, exist_ok=True)
    shutil.copytree(source, target)
    return target


def install_sidecar_runtime(target: Path, version: str, dry_run: bool) -> None:
    sidecar_venv = target / SIDECAR_VENV_NAME
    create_command = [sys.executable, "-m", "venv", str(sidecar_venv)]
    sidecar_python = sidecar_venv / "bin" / "python3"
    if not sidecar_python.exists():
        sidecar_python = sidecar_venv / "bin" / "python"
    install_command = [
        str(sidecar_python),
        "-m",
        "pip",
        "install",
        "--upgrade",
        "pip",
        "wheel",
        "setuptools",
        "convex==%s" % version,
    ]
    if dry_run:
        print("Would run: %s" % " ".join(create_command))
        print("Would run: %s" % " ".join(install_command))
        return
    subprocess.run(create_command, check=True)
    if not sidecar_python.exists():
        raise SystemExit("Sidecar Python was not created at %s" % sidecar_python)
    subprocess.run(install_command, check=True)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Install the LiveSyncRemoteScript Ableton Remote Script into a Live scripts directory."
    )
    parser.add_argument(
        "--bundle",
        action="store_true",
        help="Install into Ableton Live's app bundle instead of the User Library Remote Scripts folder.",
    )
    parser.add_argument(
        "--bundle-path",
        help="Explicit Ableton Live .app path to use with --bundle.",
    )
    parser.add_argument(
        "--target-root",
        help="Override the target Remote Scripts root directory.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Replace an existing LiveSyncRemoteScript install at the target.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print what would happen without copying files.",
    )
    parser.add_argument(
        "--list-targets",
        action="store_true",
        help="Print detected install targets and exit.",
    )
    parser.add_argument(
        "--skip-convex-install",
        action="store_true",
        help="Skip creating the sidecar venv and installing the official Convex Python client into it.",
    )
    parser.add_argument(
        "--convex-version",
        default=DEFAULT_CONVEX_VERSION,
        help="Convex Python package version to install into the target directory.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    source = source_dir()
    if not source.exists():
        raise SystemExit("Source directory not found: %s" % source)

    if args.list_targets:
        print("Source: %s" % source)
        print("User Library target: %s" % default_user_library_root())
        apps = list(detect_live_apps())
        if apps:
            for app in apps:
                print("Bundle target: %s" % (app / "Contents" / "App-Resources" / "MIDI Remote Scripts"))
        else:
            print("Bundle target: none detected under /Applications")
        return 0

    if args.target_root:
        target_root = Path(args.target_root).expanduser().resolve()
    elif args.bundle:
        target_root = resolve_bundle_root(args.bundle_path)
    else:
        target_root = default_user_library_root()

    config_path = source / "config.json"
    if not config_path.exists():
        print(
            "Warning: %s does not exist yet. Copy config.example.json to config.json before loading the script in Live."
            % config_path,
            file=sys.stderr,
        )

    installed_path = install(
        source=source,
        target_root=target_root,
        force=args.force,
        dry_run=args.dry_run,
    )

    if args.skip_convex_install:
        if args.dry_run:
            print("Would skip creating sidecar runtime in %s" % (installed_path / SIDECAR_VENV_NAME))
    else:
        install_sidecar_runtime(installed_path, args.convex_version, args.dry_run)

    if args.dry_run:
        print("Dry run complete for %s -> %s" % (SCRIPT_NAME, installed_path))
    else:
        print("Installed %s to %s" % (SCRIPT_NAME, installed_path))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
