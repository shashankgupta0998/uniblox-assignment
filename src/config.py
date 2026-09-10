"""Runtime configuration and seed data. [TAD §3.11] [D35]"""

from __future__ import annotations

import os
from dataclasses import dataclass

from src.core.models import Customer, Product


@dataclass(frozen=True)
class Config:
    n: int = 5
    x: int = 10
    admin_token: str = "dev-admin-token"
    payment_latency_seconds: float = 0.0
    currency: str = "INR"


# The six products from PRD §2.1. Prices are integer paise (minor units). [I14]
SEED_PRODUCTS: tuple[Product, ...] = (
    Product(id="prd_keyboard", name="Mechanical Keyboard", unit_price_minor=449_900, stock_total=25),
    Product(id="prd_mouse", name="Wireless Mouse", unit_price_minor=129_900, stock_total=40),
    Product(id="prd_monitor", name='27" Monitor', unit_price_minor=1_899_900, stock_total=10),
    Product(id="prd_cable", name="USB-C Cable", unit_price_minor=39_900, stock_total=100),
    Product(id="prd_dock", name="Thunderbolt Dock", unit_price_minor=2_499_900, stock_total=3),
    Product(id="prd_headset", name="Noise-Cancelling Headset", unit_price_minor=899_900, stock_total=1),
)

SEED_CUSTOMERS: tuple[Customer, ...] = (
    Customer(id="cus_1", name="Asha Rao"),
    Customer(id="cus_2", name="Bilal Khan"),
    Customer(id="cus_3", name="Chitra Nair"),
    Customer(id="cus_4", name="Dev Mehta"),
    Customer(id="cus_5", name="Esha Singh"),
)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return int(raw)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return float(raw)


def load_config() -> Config:
    """Env overrides: REWARD_N, REWARD_X, ADMIN_TOKEN, PAYMENT_LATENCY_SECONDS.  [D35]"""
    return Config(
        n=_env_int("REWARD_N", Config.n),
        x=_env_int("REWARD_X", Config.x),
        admin_token=os.environ.get("ADMIN_TOKEN") or Config.admin_token,
        payment_latency_seconds=_env_float("PAYMENT_LATENCY_SECONDS", Config.payment_latency_seconds),
    )
