"""Build a portable Laplace worker; the client does not invoke Flutter or Dart SDKs."""
import argparse
import hashlib
import json
from pathlib import Path
import os
import shutil
import subprocess
import sys
import zipfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from src.core.atomic_io import replace_file


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=ROOT / "scratch/laplace_trial/chaldea")
    parser.add_argument("--publish", action="store_true")
    args = parser.parse_args()
    source = args.source.resolve()
    flutter = shutil.which("flutter")
    if not flutter or not (source / ".dart_tool/package_config.json").exists():
        raise RuntimeError("Build requires a Flutter SDK and a prepared Chaldea source checkout with pub dependencies.")
    dest = ROOT / "runtime/laplace"
    entry = source / "lib/fgomaa"
    entry.mkdir(exist_ok=True)
    import importlib.util
    spec=importlib.util.spec_from_file_location('fgomaa_engine_patches',ROOT/'integrations/laplace/engine_patches.py')
    patches=importlib.util.module_from_spec(spec);spec.loader.exec_module(patches)
    patches.apply_engine_patches(source)
    for file in (ROOT / "integrations/laplace").glob("*.dart"):
        shutil.copy2(file, entry / file.name)
    env = dict(os.environ, PUB_CACHE=str(source.parent / "pub-cache"))
    subprocess.run([flutter, "build", "bundle", "--no-pub", "--debug", "--target-platform=windows-x64",
                    "--target=lib/fgomaa/main.dart", f"--asset-dir={dest / 'assets'}"],
                   cwd=source, env=env, check=True)
    engine = Path(flutter).resolve().parent / "cache/artifacts/engine/windows-x64"
    for name in ["flutter_tester.exe", "icudtl.dat"]:
        shutil.copy2(engine / name, dest / name)
    for file in (dest / "assets/native_assets/windows").glob("*.dll"):
        shutil.copy2(file, dest / file.name)
    licenses = dest / "licenses"
    licenses.mkdir(exist_ok=True)
    shutil.copy2(source / "LICENSE", licenses / "Chaldea-AGPL-3.0.txt")
    shutil.copy2(Path(flutter).resolve().parents[1] / "LICENSE", licenses / "Flutter-LICENSE.txt")
    for file in engine.glob("license.*.md"):
        shutil.copy2(file, licenses / file.name)
    sources = dest / "sources"
    sources.mkdir(exist_ok=True)
    upstream_zip=sources / "chaldea-source.tmp.zip"
    with zipfile.ZipFile(upstream_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        for folder in ("lib", "res"):
            for file in sorted((source / folder).rglob("*")):
                if file.is_file():
                    archive.write(file, file.relative_to(source).as_posix())
        for name in ("LICENSE", "README.md", "README.zh.md", "pubspec.yaml", "pubspec.lock", "CONTRIBUTORS"):
            if (source / name).exists():
                archive.write(source / name, name)
    replace_file(upstream_zip,sources / "chaldea-source.zip")
    for file in (ROOT / "integrations/laplace").glob("*.dart"):
        shutil.copy2(file, sources / file.name)
    adapter_zip=sources / "fgomaa-adapter-source.tmp.zip"
    with zipfile.ZipFile(adapter_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.write(Path(__file__), "scripts/build_laplace.py")
        for file in (ROOT / "integrations/laplace").iterdir():
            if file.is_file():
                archive.write(file, file.relative_to(ROOT).as_posix())
        archive.write(ROOT/'src/core/atomic_io.py','src/core/atomic_io.py')
    replace_file(adapter_zip,sources / "fgomaa-adapter-source.zip")
    revision = subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=source, text=True).strip()
    manifest = {"engine": "Chaldea Laplace", "protocol": 4, "revision": revision,
                "features": ["manual_plan_v1", "interactive_v1", "interactive_v2", "interactive_v3"],
                "source": "https://github.com/chaldea-center/chaldea",
                "kernel_sha256": hashlib.sha256((dest / "assets/kernel_blob.bin").read_bytes()).hexdigest(),
                "adapter_sha256": hashlib.sha256(b"".join(p.read_bytes() for p in sorted((ROOT / "integrations/laplace").glob("*.dart")))).hexdigest()}
    (dest / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    (dest / "NOTICE.txt").write_text(
        "Chaldea Laplace battle engine\nUpstream: " + manifest["source"] + "\nCommit: " + revision +
        "\nLicense: AGPL-3.0 (see licenses). Corresponding Chaldea source and local Dart adapter are in sources/.\n"
        "Flutter engine and third-party dependency notices are preserved in licenses/ and assets/NOTICES.Z.\n"
        "Build: python scripts/build_laplace.py --source <prepared Chaldea checkout>\n", encoding="utf-8")
    if args.publish:
        shutil.copytree(dest, ROOT / "dist/FGO-MAA/runtime/laplace", dirs_exist_ok=True)
    print("Laplace runtime built:", dest, flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
