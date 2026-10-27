from pathlib import Path

import pytest

from handpicked.config import ConfigError, config_path, load_config

EXAMPLE = Path(__file__).parent.parent / "config.example.yml"

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
    assert config.users["kid"].max_age == 10
    assert config.users["kid"].seerr_user_id == 7
    assert config.digest is None


def test_defaults(tmp_path):
    config = load_config(write(tmp_path, MINIMAL), environ={})
    assert config.dry_run is True
    assert config.picks_per_run == 3
    assert config.thresholds.completion_pct == 80
    assert config.users == {}


def test_users_are_opt_in_by_default(tmp_path):
    config = load_config(write(tmp_path, MINIMAL + "users:\n  alex: {}\n"), environ={})
    assert config.users["alex"].include is False


def test_missing_required_field(tmp_path):
    text = MINIMAL.replace("  tmdb: {token: m}\n", "")
    with pytest.raises(ConfigError, match="tmdb"):
        load_config(write(tmp_path, text), environ={})


def test_unknown_key_is_rejected(tmp_path):
    text = MINIMAL + "users:\n  alex: {include: true, max_ages: 10}\n"
    with pytest.raises(ConfigError, match="max_ages"):
        load_config(write(tmp_path, text), environ={})


def test_yaml_bool_coercion_is_rejected(tmp_path):
    # YAML 1.1 parses an unquoted `no` key as False, not the string "no".
    text = MINIMAL + "users:\n  no: {include: true}\n"
    with pytest.raises(ConfigError):
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
