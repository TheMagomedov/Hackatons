import argparse

from .config import Settings
from .shared.ports import AppError


def main() -> None:
    parser = argparse.ArgumentParser(description="Автономный AI-маркетолог")
    parser.add_argument("--check", action="store_true", help="Проверить загрузку конфигурации")
    args = parser.parse_args()
    try:
        Settings.load()
    except AppError as exc:
        parser.exit(1, f"{exc}\n")
    if args.check:
        print("Настройки и общие контракты загружены.")
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
