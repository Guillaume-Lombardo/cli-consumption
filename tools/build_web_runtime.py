from __future__ import annotations

import argparse
import json
import shutil
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).parents[1]
STANDALONE = ROOT / "apps" / "web" / ".next" / "standalone"
STATIC = ROOT / "apps" / "web" / ".next" / "static"
OUTPUT = ROOT / "src" / "cli_consumption" / "web_runtime.zip"
BUILD_ROOT_PLACEHOLDER = "/cli-consumption-build"
LICENSE_NAMES = {"license", "license.md", "license.txt", "notice", "notice.txt"}
LICENSE_FALLBACKS = {
    "@next/env": ROOT / "node_modules" / "next" / "license.md",
    "client-only": ROOT / "node_modules" / "react" / "LICENSE",
    "server-only": ROOT / "node_modules" / "react" / "LICENSE",
}
EXTRA_RUNTIME_LICENSES = {
    "@fontsource-variable/inter": ROOT
    / "node_modules"
    / "@fontsource-variable"
    / "inter"
    / "LICENSE"
}
ZIP_TIMESTAMP = (1980, 1, 1, 0, 0, 0)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--check",
        action="store_true",
        help="verify that the bundled runtime has the same uncompressed contents",
    )
    arguments = parser.parse_args()
    server = STANDALONE / "apps" / "web" / "server.js"
    if not server.is_file() or not STATIC.is_dir():
        raise SystemExit("Build the Next.js dashboard before packaging its runtime.")

    standalone_static = STANDALONE / "apps" / "web" / ".next" / "static"
    shutil.copytree(STATIC, standalone_static, dirs_exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cli-consumption-web-build-") as directory:
        staging = Path(directory) / "runtime"
        shutil.copytree(STANDALONE, staging)
        _remove_platform_specific_image_runtime(staging)
        _normalize_build_root(staging)
        _copy_runtime_licenses(staging)
        if arguments.check:
            _check_zip_contents(staging)
        else:
            _write_deterministic_zip(staging)


def _remove_platform_specific_image_runtime(staging: Path) -> None:
    modules = staging / "node_modules"
    shutil.rmtree(modules / "sharp", ignore_errors=True)
    shutil.rmtree(modules / "@img", ignore_errors=True)
    native_files = [*staging.rglob("*.node"), *staging.rglob("*.so")]
    if native_files:
        raise SystemExit("The embedded dashboard runtime contains native binaries.")


def _normalize_build_root(staging: Path) -> None:
    raw_root = str(ROOT).encode()
    escaped_root = json.dumps(str(ROOT))[1:-1].encode()
    replacement = BUILD_ROOT_PLACEHOLDER.encode()
    for path in staging.rglob("*"):
        if not path.is_file():
            continue
        contents = path.read_bytes()
        normalized = contents.replace(raw_root, replacement).replace(
            escaped_root, replacement
        )
        if raw_root in normalized or escaped_root in normalized:
            raise SystemExit(
                "The dashboard runtime contains an unnormalized build path."
            )
        if normalized != contents:
            path.write_bytes(normalized)


def _copy_runtime_licenses(staging: Path) -> None:
    licenses = staging / "THIRD_PARTY_LICENSES"
    packages: dict[str, Path] = {}
    for manifest in (staging / "node_modules").glob("*/package.json"):
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        packages[metadata["name"]] = manifest.parent
    for manifest in (staging / "node_modules").glob("@*/*/package.json"):
        metadata = json.loads(manifest.read_text(encoding="utf-8"))
        packages[metadata["name"]] = manifest.parent

    for name in sorted(packages):
        source_package = ROOT / "node_modules" / Path(*name.split("/"))
        license_file = next(
            (
                path
                for path in sorted(source_package.iterdir())
                if path.is_file() and path.name.lower() in LICENSE_NAMES
            ),
            None,
        )
        license_file = license_file or LICENSE_FALLBACKS.get(name)
        if license_file is None or not license_file.is_file():
            raise SystemExit(f"Missing runtime license for {name}.")
        target = licenses / name.replace("/", "__")
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_file, target / license_file.name)
    for name, license_file in EXTRA_RUNTIME_LICENSES.items():
        target = licenses / name.replace("/", "__")
        target.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(license_file, target / license_file.name)


def _write_deterministic_zip(staging: Path) -> None:
    temporary = OUTPUT.with_suffix(".zip.tmp")
    with zipfile.ZipFile(
        temporary,
        mode="w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=9,
    ) as archive:
        for path in sorted(staging.rglob("*")):
            if not path.is_file() or path.suffix == ".map":
                continue
            relative = Path("runtime") / path.relative_to(staging)
            information = zipfile.ZipInfo(relative.as_posix(), ZIP_TIMESTAMP)
            information.compress_type = zipfile.ZIP_DEFLATED
            information.external_attr = 0o100644 << 16
            archive.writestr(information, path.read_bytes(), compresslevel=9)
    temporary.replace(OUTPUT)


def _check_zip_contents(staging: Path) -> None:
    expected = {
        (Path("runtime") / path.relative_to(staging)).as_posix(): path
        for path in sorted(staging.rglob("*"))
        if path.is_file() and path.suffix != ".map"
    }
    try:
        with zipfile.ZipFile(OUTPUT) as archive:
            if sorted(archive.namelist()) != sorted(expected):
                archive_names = set(archive.namelist())
                missing = sorted(set(expected) - archive_names)[:10]
                unexpected = sorted(archive_names - set(expected))[:10]
                raise SystemExit(
                    "The bundled dashboard runtime file list is out of date: "
                    f"missing={missing!r}, unexpected={unexpected!r}."
                )
            changed = [
                name
                for name, path in expected.items()
                if archive.read(name) != path.read_bytes()
            ][:10]
            if changed:
                raise SystemExit(
                    "The bundled dashboard runtime contents are out of date: "
                    f"changed={changed!r}."
                )
    except (FileNotFoundError, zipfile.BadZipFile) as error:
        raise SystemExit("The bundled dashboard runtime is invalid.") from error


if __name__ == "__main__":
    main()
