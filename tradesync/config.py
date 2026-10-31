"""Loads config.toml and the database URL."""
import os
import tomllib
from dataclasses import dataclass
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

# Local-dev default that matches docker-compose.yml. Real passwords go in .env / DATABASE_URL.
DEFAULT_DATABASE_URL = "postgresql://tradesync:tradesync@localhost:5432/tradesync"


@dataclass(frozen=True)
class Config:
    price_tolerance: Decimal
    data_dir: Path
    internal_file: Path
    custodian_file: Path
    ground_truth_file: Path
    seed: int
    size: int


def load_config(path: Path | str = ROOT / "config.toml") -> Config:
    with open(path, "rb") as f:
        raw = tomllib.load(f)
    data_dir = ROOT / raw["data"]["dir"]
    return Config(
        # Decimal(str) keeps the tolerance exact, the same reason money uses NUMERIC in SQL.
        price_tolerance=Decimal(str(raw["recon"]["price_tolerance"])),
        data_dir=data_dir,
        internal_file=data_dir / raw["data"]["internal_file"],
        custodian_file=data_dir / raw["data"]["custodian_file"],
        ground_truth_file=data_dir / raw["data"]["ground_truth_file"],
        seed=int(raw["generator"]["seed"]),
        size=int(raw["generator"]["size"]),
    )


def read_env_file(path: Path = ROOT / ".env") -> dict[str, str]:
    """Minimal KEY=VALUE reader, so the password set in .env for Docker also reaches Python.

    Hand-written instead of python-dotenv to keep the dependency list unchanged.
    """
    values = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, _, value = line.partition("=")
                values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def database_url() -> str:
    # A real environment variable wins (CI, or a one-off override), then .env, then the dev default.
    return (os.environ.get("DATABASE_URL")
            or read_env_file().get("DATABASE_URL")
            or DEFAULT_DATABASE_URL)
