"""Shared fixtures.

Tests load the real config/*.yaml rather than hand-built objects. A test that
passes against a fabricated config proves the code works on a config nobody
ships; loading the real one also means a bad edit to the YAML fails the suite.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.classifier.rules import RulesClassifier
from app.config_loader import load_config
from app.engine.compiler import compile_rules

CONFIG_DIR = Path(__file__).resolve().parent.parent / "config"


@pytest.fixture(scope="session")
def config():
    return load_config(CONFIG_DIR)


@pytest.fixture(scope="session")
def classifier(config):
    return RulesClassifier(config.intents)


@pytest.fixture(scope="session")
def compiled(config):
    return compile_rules(config.personalization)


@pytest.fixture
def profile() -> dict:
    return {
        "id": "user_101",
        "name": "Aarav Sharma",
        "language": "en",
        "subscription": "premium",
        "tonePreference": "motivational",
        "birthDetails": {"date": "1997-08-15", "time": "09:35", "place": "Delhi"},
    }


@pytest.fixture
def kundli() -> dict:
    return {
        "lagna": "Libra",
        "moonSign": "Scorpio",
        "currentDasha": {"mahadasha": "Rahu", "antardasha": "Mars"},
        "houses": {
            "6": {"lord": "Jupiter", "strength": "Average"},
            "7": {"lord": "Mars", "strength": "Weak"},
            "10": {"lord": "Moon", "strength": "Strong"},
        },
    }


@pytest.fixture
def horoscope() -> dict:
    return {
        "career": "Networking may bring new opportunities.",
        "finance": "Avoid risky investments.",
        "health": "Prioritize proper sleep.",
        "relationship": "Communication with your partner improves.",
    }


@pytest.fixture
def panchang() -> dict:
    return {
        "date": "2026-08-01",
        "tithi": "Shukla Panchami",
        "nakshatra": "Rohini",
        "yoga": "Siddhi",
        "karana": "Bava",
    }
