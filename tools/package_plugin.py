"""Build the QGIS installation archive from the repository sources."""
from configparser import ConfigParser
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "magoh_spell"


def main():
    metadata = ConfigParser(interpolation=None)
    metadata.read(PLUGIN / "metadata.txt", encoding="utf-8")
    version = metadata["general"]["version"]
    archive = ROOT / "dist" / f"MAGOH_Spell_V{version}.zip"
    archive.parent.mkdir(exist_ok=True)
    with ZipFile(archive, "w", compression=ZIP_DEFLATED) as package:
        for path in sorted(PLUGIN.rglob("*")):
            if not path.is_file() or "__pycache__" in path.parts:
                continue
            if path.name in {".DS_Store", "Thumbs.db"} or path.suffix in {
                ".pyc", ".pyo", ".gpkg-wal", ".gpkg-shm"
            }:
                continue
            package.write(path, path.relative_to(ROOT))
    print(archive)


if __name__ == "__main__":
    main()
