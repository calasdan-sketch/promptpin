"""Project configuration (``promptpin.yaml``) and path helpers."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import yaml

from promptpin.errors import ConfigError

CONFIG_FILENAME = "promptpin.yaml"
LOCK_FILENAME = "promptpin.lock"
STATE_DIRNAME = ".promptpin"
DB_FILENAME = "state.db"
DEPLOY_LOCK_FILENAME = "deploy.lock"


@dataclass
class Config:
    prompts_dir: str = "prompts"
    envs: list[str] = field(default_factory=lambda: ["dev", "staging", "production"])
    log_sample_rate: float = 1.0
    auto_commit_deploys: bool = True

    @classmethod
    def load(cls, repo_root: Path) -> Config:
        path = repo_root / CONFIG_FILENAME
        if not path.exists():
            return cls()
        try:
            data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        except yaml.YAMLError as exc:
            raise ConfigError(f"{CONFIG_FILENAME}: invalid YAML: {exc}") from exc
        if not isinstance(data, dict):
            raise ConfigError(f"{CONFIG_FILENAME}: top-level document must be a mapping")
        try:
            sample_rate = float(data.get("log_sample_rate", 1.0))
        except (TypeError, ValueError):
            raise ConfigError(f"{CONFIG_FILENAME}: log_sample_rate must be a number") from None
        if not 0.0 <= sample_rate <= 1.0:
            raise ConfigError(f"{CONFIG_FILENAME}: log_sample_rate must be between 0 and 1")
        envs = data.get("envs", ["dev", "staging", "production"])
        if not isinstance(envs, list) or not envs or any(not isinstance(e, str) for e in envs):
            # `envs: production` would otherwise become ['p','r','o',...].
            raise ConfigError(f"{CONFIG_FILENAME}: envs must be a non-empty list of names")
        prompts_dir = str(data.get("prompts_dir", "prompts")).replace("\\", "/").rstrip("/")
        parts = prompts_dir.split("/")
        if (
            not prompts_dir
            or prompts_dir.startswith("/")
            or (len(prompts_dir) > 1 and prompts_dir[1] == ":")
            or ".." in parts
        ):
            raise ConfigError(
                f"{CONFIG_FILENAME}: prompts_dir must be a relative path inside the repo"
            )
        return cls(
            prompts_dir=prompts_dir,
            envs=list(envs),
            log_sample_rate=sample_rate,
            auto_commit_deploys=bool(data.get("auto_commit_deploys", True)),
        )

    def dump(self) -> str:
        return yaml.safe_dump(
            {
                "prompts_dir": self.prompts_dir,
                "envs": self.envs,
                "log_sample_rate": self.log_sample_rate,
                "auto_commit_deploys": self.auto_commit_deploys,
            },
            sort_keys=False,
        )


def state_dir(repo_root: Path) -> Path:
    return repo_root / STATE_DIRNAME


def db_path(repo_root: Path) -> Path:
    return state_dir(repo_root) / DB_FILENAME


def deploy_lock_path(repo_root: Path) -> Path:
    return state_dir(repo_root) / DEPLOY_LOCK_FILENAME


def ensure_state_dir(repo_root: Path) -> Path:
    d = state_dir(repo_root)
    d.mkdir(parents=True, exist_ok=True)
    return d
