from pathlib import Path

import pytest

from handpicked.config import ConfigError, config_path, load_config

EXAMPLE = Path(__file__).parent.parent / "src" / "handpicked" / "config.example.yml"

MINIMAL = """
services:
  tautulli: {url: http://tautulli:8181, api_key: t}
  seerr: {url: http://seerr:5055, api_key: s}
  tmdb: {token: m}
"""


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "config.yml"
    path.write_text(text)
    return path


def test_example_config_is_valid():
    config = load_config(EXAMPLE, environ={})
    assert config.dry_run is True
    assert config.digest is None


def test_defaults(tmp_path):
    config = load_config(write(tmp_path, MINIMAL), environ={})
    assert config.dry_run is True
    assert config.picks_per_run == 3
    assert config.thresholds.completion_pct == 80


def test_missing_required_field(tmp_path):
    text = MINIMAL.replace("  tmdb: {token: m}\n", "")
    with pytest.raises(ConfigError, match="tmdb"):
        load_config(write(tmp_path, text), environ={})


def test_unknown_key_is_rejected(tmp_path):
    text = MINIMAL + "thresholds: {min_vote_counts: 10}\n"
    with pytest.raises(ConfigError, match="min_vote_counts"):
        load_config(write(tmp_path, text), environ={})


def test_users_section_is_rejected(tmp_path):
    # Users moved to the database; an old config should fail loudly.
    text = MINIMAL + "users:\n  alex: {include: true}\n"
    with pytest.raises(ConfigError, match="users"):
        load_config(write(tmp_path, text), environ={})


def test_env_overrides_secrets(tmp_path):
    environ = {
        "HANDPICKED_TAUTULLI_API_KEY": "env-t",
        "HANDPICKED_SEERR_API_KEY": "env-s",
        "HANDPICKED_TMDB_TOKEN": "env-m",
    }
    config = load_config(write(tmp_path, MINIMAL), environ=environ)
    assert config.services.tautulli.api_key == "env-t"
    assert config.services.seerr.api_key == "env-s"
    assert config.services.tmdb.token == "env-m"


def test_env_supplies_secret_missing_from_file(tmp_path):
    text = MINIMAL.replace("{token: m}", "{}")
    config = load_config(write(tmp_path, text), environ={"HANDPICKED_TMDB_TOKEN": "m"})
    assert config.services.tmdb.token == "m"


def test_empty_env_value_does_not_override(tmp_path):
    config = load_config(
        write(tmp_path, MINIMAL), environ={"HANDPICKED_TMDB_TOKEN": ""}
    )
    assert config.services.tmdb.token == "m"


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(tmp_path / "nope.yml", environ={})


def test_non_mapping_file(tmp_path):
    with pytest.raises(ConfigError, match="mapping"):
        load_config(write(tmp_path, "- a\n- b\n"), environ={})


def test_config_path_from_env():
    assert config_path({}) == Path("/config/config.yml")
    assert config_path({"HANDPICKED_CONFIG": "dev/config.yml"}) == Path(
        "dev/config.yml"
    )


@pytest.mark.parametrize(
    "schedule", ["0 9 * * SUN", "30 6 * * MON-FRI", "0 9 1 * *", "0 9 */2 * MON"]
)
def test_schedule_accepts_cron_with_day_names(tmp_path, schedule):
    text = MINIMAL + f'schedule: "{schedule}"\n'

    assert load_config(write(tmp_path, text), environ={}).schedule == schedule


@pytest.mark.parametrize(
    ("schedule", "message"),
    [
        ("0 9 * * 0", "name the day of the week"),
        ("0 9 * * 1-5", "name the day of the week"),
        ("0 9 * * 7", "name the day of the week"),
        ("0 9 1,15 * MON", "not both"),
        ("0 9 * *", "not a valid cron expression"),
        ("0 25 * * SUN", "not a valid cron expression"),
        ("0 9 ? * MON", "not a valid cron expression"),
    ],
)
def test_schedule_rejects_numeric_days_and_bad_cron(tmp_path, schedule, message):
    text = MINIMAL + f'schedule: "{schedule}"\n'

    with pytest.raises(ConfigError, match=message):
        load_config(write(tmp_path, text), environ={})
