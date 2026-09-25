import os
import pytest

from src.core.state import get_cloud_temperature, get_local_temperature


def test_cloud_temperature_defaults(monkeypatch):
    for var in ["CLOUD_TOOL_TEMPERATURE", "TOOL_TEMPERATURE", "CLOUD_CHAT_TEMPERATURE", "CHAT_TEMPERATURE_CLOUD", "CLOUD_TEMPERATURE", "CHAT_TEMPERATURE"]:
        monkeypatch.delenv(var, raising=False)
    assert get_cloud_temperature() == 0.7


def test_cloud_temperature_explicit(monkeypatch):
    monkeypatch.setenv("CLOUD_CHAT_TEMPERATURE", "0.75")
    assert get_cloud_temperature() == 0.75

    monkeypatch.delenv("CLOUD_CHAT_TEMPERATURE")
    monkeypatch.setenv("CHAT_TEMPERATURE_CLOUD", "0.8")
    assert get_cloud_temperature() == 0.8

    monkeypatch.delenv("CHAT_TEMPERATURE_CLOUD")
    monkeypatch.setenv("CLOUD_TEMPERATURE", "0.65")
    assert get_cloud_temperature() == 0.65


def test_cloud_temperature_fallback_to_chat_temperature(monkeypatch):
    for var in ["CLOUD_CHAT_TEMPERATURE", "CHAT_TEMPERATURE_CLOUD", "CLOUD_TEMPERATURE"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHAT_TEMPERATURE", "0.7")
    assert get_cloud_temperature() == 0.7


def test_local_temperature_defaults(monkeypatch):
    for var in ["LOCAL_TOOL_TEMPERATURE", "TOOL_TEMPERATURE", "LOCAL_CHAT_TEMPERATURE", "CHAT_TEMPERATURE_LOCAL", "LOCAL_TEMPERATURE", "CHAT_TEMPERATURE"]:
        monkeypatch.delenv(var, raising=False)
    assert get_local_temperature() == 0.6


def test_local_temperature_explicit(monkeypatch):
    monkeypatch.setenv("LOCAL_CHAT_TEMPERATURE", "0.55")
    assert get_local_temperature() == 0.55

    monkeypatch.delenv("LOCAL_CHAT_TEMPERATURE")
    monkeypatch.setenv("CHAT_TEMPERATURE_LOCAL", "0.62")
    assert get_local_temperature() == 0.62

    monkeypatch.delenv("CHAT_TEMPERATURE_LOCAL")
    monkeypatch.setenv("LOCAL_TEMPERATURE", "0.58")
    assert get_local_temperature() == 0.58


def test_local_temperature_fallback_to_chat_temperature(monkeypatch):
    for var in ["LOCAL_CHAT_TEMPERATURE", "CHAT_TEMPERATURE_LOCAL", "LOCAL_TEMPERATURE"]:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("CHAT_TEMPERATURE", "0.6")
    assert get_local_temperature() == 0.6


def test_tool_temperature_overrides(monkeypatch):
    monkeypatch.setenv("CLOUD_CHAT_TEMPERATURE", "0.5")
    monkeypatch.setenv("LOCAL_CHAT_TEMPERATURE", "0.4")
    monkeypatch.setenv("TOOL_TEMPERATURE", "0.85")

    # In regular mode, normal chat temperatures apply
    assert get_cloud_temperature(tool_mode=False) == 0.5
    assert get_local_temperature(tool_mode=False) == 0.4

    # In tool mode, TOOL_TEMPERATURE overrides
    assert get_cloud_temperature(tool_mode=True) == 0.85
    assert get_local_temperature(tool_mode=True) == 0.85

    # Specific provider tool temperatures have highest precedence
    monkeypatch.setenv("CLOUD_TOOL_TEMPERATURE", "0.9")
    monkeypatch.setenv("LOCAL_TOOL_TEMPERATURE", "0.75")
    assert get_cloud_temperature(tool_mode=True) == 0.9
    assert get_local_temperature(tool_mode=True) == 0.75


def test_invalid_temperature_values(monkeypatch):
    monkeypatch.setenv("CLOUD_CHAT_TEMPERATURE", "invalid_number")
    assert get_cloud_temperature() == 0.7

    monkeypatch.setenv("LOCAL_CHAT_TEMPERATURE", "invalid_number")
    assert get_local_temperature() == 0.6
