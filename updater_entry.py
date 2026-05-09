from __future__ import annotations

import sys

from updater import UPDATE_ARGUMENT, handle_update_cli


def main() -> int:
    update_result = handle_update_cli(sys.argv)
    if update_result is None:
        print(f"Usage: updater.exe {UPDATE_ARGUMENT} <update-state.json>", file=sys.stderr)
        return 2
    return update_result


if __name__ == "__main__":
    raise SystemExit(main())
