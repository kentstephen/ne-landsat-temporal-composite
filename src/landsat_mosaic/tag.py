"""Tag the main branch of the local Icechunk repo.

  uv run python src/landsat_mosaic/tag.py v3

Idempotent: an existing tag at the same snapshot is a no-op; an existing tag
at a different snapshot is an error (tags are immutable, pick a new name).
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from landsat_mosaic import init_store


def main() -> None:
    name = sys.argv[1]
    repo = init_store.open_repo()
    head = repo.lookup_branch("main")
    if name in repo.list_tags():
        at = repo.lookup_tag(name)
        if at == head:
            print(f"tag {name} already at main ({head})")
            return
        sys.exit(f"tag {name} exists at {at}, main is {head}; tags are immutable, use a new name")
    repo.create_tag(name, head)
    print(f"tagged {name} = {head}")


if __name__ == "__main__":
    main()
