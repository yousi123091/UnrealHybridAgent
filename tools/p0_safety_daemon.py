"""Deprecated standalone safety authority; intentionally refuses to start.

Use the runtime-owned SafetyController and guarded_input worker/guardian.
A second controller writing the same gate can overwrite the live owner state.
"""
import sys

def main() -> int:
    print("Standalone safety daemon disabled: start UHA normally; its input worker owns the release guardian.", file=sys.stderr)
    return 2

if __name__ == '__main__':
    raise SystemExit(main())
