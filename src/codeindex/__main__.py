"""Entry point for `python -m codeindex` and `codeindex` console script."""

from .cli.commands import run_cli


def main():
    run_cli()


if __name__ == "__main__":
    main()
