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


def database_url() -> str:
    return os.environ.get("DATABASE_URL", DEFAULT_DATABASE_URL)
