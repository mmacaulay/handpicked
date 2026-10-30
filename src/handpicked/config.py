"""Load and validate config.yml."""

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Literal

import yaml
from apscheduler.triggers.cron import CronTrigger
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    ValidationError,
    field_validator,
)

DEFAULT_CONFIG_PATH = Path("/config/config.yml")

# Secrets that can come from the environment (e.g. the Unraid template)
# instead of the config file. Env values win over file values.
SECRET_ENV_VARS = {
    "HANDPICKED_TAUTULLI_API_KEY": ("services", "tautulli", "api_key"),
    "HANDPICKED_SEERR_API_KEY": ("services", "seerr", "api_key"),
    "HANDPICKED_TMDB_TOKEN": ("services", "tmdb", "token"),
    "HANDPICKED_SMTP_PASSWORD": ("digest", "password"),
}


class ConfigError(Exception):
    pass


class Model(BaseModel):
    # Hand-edited file: reject unknown keys so typos fail loudly.
    model_config = ConfigDict(extra="forbid")


class Service(Model):
    url: str
    api_key: str


class SeerrService(Service):
    # The local Seerr user Handpicked creates requests as. Needed only when
    # dry_run is off.
    request_user_id: int | None = None


class Tmdb(Model):
    token: str


class Services(Model):
    tautulli: Service
    seerr: SeerrService
    tmdb: Tmdb


class Thresholds(Model):
    completion_pct: int = Field(80, ge=0, le=100)
    abandon_pct: int = Field(25, ge=0, le=100)
    seed_window_days: int = Field(90, ge=1)
    min_seeds: int = Field(3, ge=1)
    min_vote_count: int = Field(100, ge=0)
    tv_min_finished_episodes: int = Field(3, ge=1)
    tv_abandon_weeks: int = Field(4, ge=1)


class Digest(Model):
    host: str
    port: int | None = None  # 465 for ssl, 587 for starttls, 25 for none
    security: Literal["starttls", "ssl", "none"] = "starttls"
    username: str | None = None
    password: str | None = None
    sender: str
    recipients: list[str] = Field(min_length=1)


class Config(Model):
    services: Services
    dry_run: bool = True
    schedule: str = "0 9 * * SUN"
    picks_per_run: int = Field(3, ge=1)
    thresholds: Thresholds = Field(default_factory=Thresholds)
    digest: Digest | None = None

    @field_validator("schedule")
    @classmethod
    def _check_schedule(cls, schedule: str) -> str:
        fields = schedule.split()
        if len(fields) == 5 and any(c.isdigit() for c in fields[4]):
            raise ValueError(
                "name the day of the week (SUN, MON-FRI): the scheduler counts"
                " 0 as Monday, not Sunday"
            )
        try:
            CronTrigger.from_crontab(schedule, timezone="UTC")
        except ValueError as e:
            raise ValueError(f"not a valid cron expression: {e}") from e
        return schedule


def config_path(environ: Mapping[str, str] = os.environ) -> Path:
    return Path(environ.get("HANDPICKED_CONFIG", DEFAULT_CONFIG_PATH))


def load_config(path: Path, environ: Mapping[str, str] = os.environ) -> Config:
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except OSError as e:
        raise ConfigError(f"Cannot read config file {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")

    _apply_env_overrides(raw, environ)

    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Invalid config in {path}:\n{e}") from e


def _apply_env_overrides(raw: dict, environ: Mapping[str, str]) -> None:
    for var, keys in SECRET_ENV_VARS.items():
        value = environ.get(var)
        if not value:
            continue
        node = raw
        for key in keys[:-1]:
            if node.get(key) is None:
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value
