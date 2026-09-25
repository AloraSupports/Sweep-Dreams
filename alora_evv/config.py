"""Loads settings.yaml, rules.yaml and state_form.yaml."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

import yaml

from .paths import CONFIG_DIR


def _load(name: str, folder: Path | None = None) -> dict:
    path = (folder or CONFIG_DIR) / name
    with open(path, encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@dataclass
class Config:
    settings: dict
    rules: dict
    form: dict

    # -- providers ---------------------------------------------------------
    def provider_by_medicaid_id(self, medicaid_id: str) -> dict | None:
        mid = (medicaid_id or "").strip()
        for p in self.settings.get("providers", []):
            if str(p["medicaid_id"]) == mid:
                return p
        return None

    # -- rules ---------------------------------------------------------------
    def error_type(self, key: str) -> dict | None:
        return self.rules.get("error_types", {}).get(key)

    def reason(self, code: str | None) -> dict | None:
        if code is None:
            return None
        return self.rules.get("reason_codes", {}).get(str(code))

    def programs_for_code(self, code: str) -> list[str]:
        return [name for name, prog in self.rules.get("programs", {}).items()
                if str(code) in {str(k) for k in prog.get("services", {})}]

    def program(self, name: str) -> dict | None:
        return self.rules.get("programs", {}).get(name)

    def allowed_decisions(self) -> set[str]:
        return set(self.rules.get("error_types", {})) | set(self.rules.get("decision_extras", []))


def load_config(folder: Path | None = None) -> Config:
    return Config(settings=_load("settings.yaml", folder),
                  rules=_load("rules.yaml", folder),
                  form=_load("state_form.yaml", folder))
