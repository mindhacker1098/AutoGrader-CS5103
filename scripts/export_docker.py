"""Export the prebuilt images and launchers as one transferable ZIP (no user data)."""
from __future__ import annotations

import argparse
import hashlib
import shutil
import subprocess
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
IMAGES = ("autograder-improved:2.0.0", "autograder-improved-sandbox:2.0.0")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "release")
    args = parser.parse_args()
    output = args.output.resolve()
    bundle = output / "autograder-lab"
    bundle.mkdir(parents=True, exist_ok=True)
    for name in ("Start-AutoGrader.cmd", "start-autograder.sh", "RUN-ME.txt"):
        shutil.copyfile(ROOT / "docker" / name, bundle / name)
    shutil.copyfile(ROOT / "compose.yaml", bundle / "compose.yaml")
    print("Saving application and sandbox images...", flush=True)
    subprocess.run(["docker", "image", "inspect", *IMAGES], check=True, stdout=subprocess.DEVNULL)
    subprocess.run(["docker", "save", "-o", str(bundle / "autograder-images.tar"), *IMAGES], check=True)
    archive = output / "autograder-lab.zip"
    print("Compressing portable ZIP...", flush=True)
    with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as target:
        for path in sorted(bundle.iterdir()):
            target.write(path, f"autograder-lab/{path.name}")
    digest = hashlib.sha256()
    with archive.open("rb") as stream:
        for chunk in iter(lambda: stream.read(4 * 1024 * 1024), b""):
            digest.update(chunk)
    (output / "autograder-lab.zip.sha256").write_text(f"{digest.hexdigest()}  {archive.name}\n", encoding="ascii")
    print(f"Created {archive} ({archive.stat().st_size / 1024**2:.1f} MiB)", flush=True)


if __name__ == "__main__":
    main()
