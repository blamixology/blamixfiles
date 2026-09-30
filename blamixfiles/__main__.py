"""`python -m blamixfiles` opens the desktop app; with arguments it runs the CLI."""
import sys

if __name__ == "__main__":
    if len(sys.argv) > 1:
        from .cli import main
    else:
        from .main import main
    main()
