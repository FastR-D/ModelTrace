"""Copy web assets to Vercel's CDN directory without changing source assets."""

from pathlib import Path
import shutil


def main():
    project = Path(__file__).resolve().parent
    source = project / "static"
    destination = project / "public" / "static"
    if destination.is_symlink() or destination.parent.is_symlink():
        raise RuntimeError("Refusing to build into a symlink")
    shutil.copytree(source, destination, dirs_exist_ok=True)
    print("Prepared public/static for Vercel")


if __name__ == "__main__":
    main()
