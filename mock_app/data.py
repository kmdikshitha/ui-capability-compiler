"""Seeded members and tenant config for the mock teller console.

All members are invented. No real names, no real PII, no real account numbers.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

TENANT_DIR = Path(__file__).parent / "tenants"

# id -> record. Balances are invented and deliberately small.
MEMBERS: dict[str, dict[str, Any]] = {
    "12345": {"name": "A. Rivera", "status": "Active", "branch": "Eastgate",
              "accounts": [{"type": "Checking", "number": "CK-0011", "balance": "1,240.55"},
                           {"type": "Savings", "number": "SV-0042", "balance": "8,915.20"}]},
    "23456": {"name": "B. Okafor", "status": "Active", "branch": "Westfield",
              "accounts": [{"type": "Checking", "number": "CK-0102", "balance": "420.00"}]},
    "34567": {"name": "C. Lindqvist", "status": "Active", "branch": "Eastgate",
              "accounts": [{"type": "Savings", "number": "SV-0113", "balance": "15,002.75"}]},
    "45678": {"name": "D. Mbeki", "status": "Active", "branch": "Northpoint",
              "accounts": [{"type": "Checking", "number": "CK-0204", "balance": "77.10"}]},
    "54321": {"name": "E. Castellano", "status": "Active", "branch": "Westfield",
              "accounts": [{"type": "Checking", "number": "CK-0300", "balance": "3,310.40"},
                           {"type": "Savings", "number": "SV-0301", "balance": "620.00"}]},
    "55555": {"name": "R. Solano", "status": "Restricted", "branch": "Northpoint",
              "restricted": True,
              "accounts": [{"type": "Checking", "number": "CK-0500", "balance": "0.00"}]},
    "56789": {"name": "F. Adeyemi", "status": "Active", "branch": "Eastgate",
              "accounts": [{"type": "Savings", "number": "SV-0567", "balance": "2,048.00"}]},
    "61234": {"name": "G. Petrov", "status": "Active", "branch": "Southbank",
              "accounts": [{"type": "Checking", "number": "CK-0612", "balance": "980.65"}]},
    "67890": {"name": "H. Nakamura", "status": "Active", "branch": "Southbank",
              "accounts": [{"type": "Savings", "number": "SV-0678", "balance": "11,430.00"}]},
    "71234": {"name": "I. Traore", "status": "Active", "branch": "Eastgate",
              "accounts": [{"type": "Checking", "number": "CK-0712", "balance": "245.99"}]},
    "72345": {"name": "J. Halvorsen", "status": "Dormant", "branch": "Westfield",
              "accounts": [{"type": "Savings", "number": "SV-0723", "balance": "5.00"}]},
    "78901": {"name": "K. Duarte", "status": "Active", "branch": "Northpoint",
              "accounts": [{"type": "Checking", "number": "CK-0789", "balance": "1,876.30"}]},
    "81234": {"name": "L. Marchetti", "status": "Active", "branch": "Southbank",
              "accounts": [{"type": "Savings", "number": "SV-0812", "balance": "640.15"}]},
    "83456": {"name": "M. Osei", "status": "Active", "branch": "Eastgate",
              "accounts": [{"type": "Checking", "number": "CK-0834", "balance": "12,001.00"}]},
    "89012": {"name": "N. Bergstrom", "status": "Active", "branch": "Westfield",
              "accounts": [{"type": "Savings", "number": "SV-0890", "balance": "333.33"}]},
    "90123": {"name": "O. Kaur", "status": "Active", "branch": "Northpoint",
              "accounts": [{"type": "Checking", "number": "CK-0901", "balance": "4,500.00"}]},
    "91234": {"name": "P. Ferreira", "status": "Active", "branch": "Southbank",
              "accounts": [{"type": "Savings", "number": "SV-0912", "balance": "89.90"}]},
    "92345": {"name": "Q. Whitfield", "status": "Active", "branch": "Eastgate",
              "accounts": [{"type": "Checking", "number": "CK-0923", "balance": "725.00"}]},
    "93456": {"name": "S. Andersson", "status": "Active", "branch": "Westfield",
              "accounts": [{"type": "Savings", "number": "SV-0934", "balance": "19,220.80"}]},
    "94567": {"name": "T. Iqbal", "status": "Active", "branch": "Northpoint",
              "accounts": [{"type": "Checking", "number": "CK-0945", "balance": "1,105.45"}]},
}

# 99999 is deliberately absent -> "No record found" business outcome.
# 55555 is present but restricted -> permission-denied business outcome.

ACCOUNT_TYPES = ["Savings", "Checking", "Money Market", "Certificate"]


def get_member(member_id: str) -> dict[str, Any] | None:
    return MEMBERS.get(member_id)


def load_tenant(name: str) -> dict[str, Any]:
    """Load a tenant config. Unknown names fall back to heritage."""
    path = TENANT_DIR / f"{name}.yaml"
    if not path.exists():
        path = TENANT_DIR / "heritage.yaml"
    return yaml.safe_load(path.read_text())
