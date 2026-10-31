"""Load and validate the config, from the environment and config.yml."""

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
    model_validator,
)

DEFAULT_CONFIG_PATH = Path("/config/config.yml")

# Settings that can come from the environment (the Unraid template's fields)
# instead of the config file. Env values win over file values.
ENV_VARS = {
    "HANDPICKED_TAUTULLI_URL": ("services", "tautulli", "url"),
    "HANDPICKED_TAUTULLI_API_KEY": ("services", "tautulli", "api_key"),
    "HANDPICKED_SEERR_URL": ("services", "seerr", "url"),
    "HANDPICKED_SEERR_API_KEY": ("services", "seerr", "api_key"),
    "HANDPICKED_TMDB_TOKEN": ("services", "tmdb", "token"),
    "HANDPICKED_SCHEDULE": ("schedule",),
    "HANDPICKED_PICKS_PER_RUN": ("picks_per_run",),
    "HANDPICKED_SMTP_HOST": ("digest", "host"),
    "HANDPICKED_SMTP_USERNAME": ("digest", "username"),
    "HANDPICKED_SMTP_PASSWORD": ("digest", "password"),
    "HANDPICKED_DIGEST_RECIPIENTS": ("digest", "recipients"),
}
LIST_ENV_VARS = {"HANDPICKED_DIGEST_RECIPIENTS"}  # comma-separated

# Named as in the Unraid template, for the error when one is missing. The
# digest's are required only once any digest setting is given.
REQUIRED = {
    ("services", "tautulli", "url"): "Tautulli URL",
    ("services", "tautulli", "api_key"): "Tautulli API key",
    ("services", "seerr", "url"): "Seerr URL",
    ("services", "seerr", "api_key"): "Seerr API key",
    ("services", "tmdb", "token"): "TMDB read access token",
}
REQUIRED_FOR_DIGEST = {
    ("digest", "host"): "SMTP host",
    ("digest", "recipients"): "Digest recipients",
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
    sender: str | None = None  # defaults to username
    recipients: list[str] = Field(min_length=1)

    @model_validator(mode="after")
    def _default_sender(self) -> Digest:
        if self.sender is None:
            if self.username is None:
                raise ValueError("set a sender, or a username to send as")
            self.sender = self.username
        return self


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
        # Before parsing, which rejects 7 (Sunday in cron) as out of range.
        if len(fields) == 5 and any(c.isdigit() for c in fields[4]):
            raise ValueError(
                "name the day of the week (SUN, MON-FRI): the scheduler counts"
                " 0 as Monday, not Sunday"
            )
        try:
            CronTrigger.from_crontab(schedule, timezone="UTC")
        except ValueError as e:
            raise ValueError(f"not a valid cron expression: {e}") from e
        _, _, day, _, weekday = fields
        # Cron runs on either day when both are restricted; the scheduler
        # needs both to match. They agree when either starts with "*".
        if not day.startswith("*") and not weekday.startswith("*"):
            raise ValueError(
                "set a day of the month or a day of the week, not both: the"
                " scheduler requires both to match, where cron needs either"
            )
        return schedule


def config_path(environ: Mapping[str, str] = os.environ) -> Path:
    return Path(environ.get("HANDPICKED_CONFIG", DEFAULT_CONFIG_PATH))


def load_config(path: Path, environ: Mapping[str, str] = os.environ) -> Config:
    """Load the config. The file is optional: the environment can set it all."""
    try:
        raw = yaml.safe_load(path.read_text()) or {}
    except FileNotFoundError:
        raw = {}
    except OSError as e:
        raise ConfigError(f"Cannot read config file {path}: {e}") from e
    except yaml.YAMLError as e:
        raise ConfigError(f"Invalid YAML in {path}: {e}") from e

    if not isinstance(raw, dict):
        raise ConfigError(f"{path} must contain a mapping at the top level")

    _apply_env_overrides(raw, environ)
    _check_required(raw, path)

    try:
        return Config.model_validate(raw)
    except ValidationError as e:
        raise ConfigError(f"Invalid config in {path}:\n{e}") from e


def _apply_env_overrides(raw: dict, environ: Mapping[str, str]) -> None:
    for var, keys in ENV_VARS.items():
        # Unraid passes a template field left blank as an empty value.
        value: str | list[str] | None = environ.get(var)
        if not value:
            continue
        if var in LIST_ENV_VARS:
            value = [item.strip() for item in value.split(",") if item.strip()]
        node = raw
        for key in keys[:-1]:
            if node.get(key) is None:
                node[key] = {}
            node = node[key]
        node[keys[-1]] = value


def _check_required(raw: dict, path: Path) -> None:
    """Name missing settings as the template does, before pydantic would."""
    required = dict(REQUIRED)
    if isinstance(raw.get("digest"), dict):
        required |= REQUIRED_FOR_DIGEST
    missing = [label for keys, label in required.items() if not _lookup(raw, keys)]
    if not missing:
        return
    where = path if path.exists() else f"{path} (not found)"
    raise ConfigError(
        f"Missing settings: {', '.join(missing)}. Fill them in on the"
        f" container's template, or set them in {where}."
    )


def _lookup(raw: dict, keys: tuple[str, ...]) -> object:
    node: object = raw
    for key in keys:
        if not isinstance(node, dict):
            return None
        node = node.get(key)
    return node
