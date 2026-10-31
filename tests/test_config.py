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
    assert config.dry_run is False
    assert config.digest is None


def test_defaults(tmp_path):
    config = load_config(write(tmp_path, MINIMAL), environ={})
    assert config.dry_run is False
    assert config.picks_per_run == 3
    assert config.thresholds.completion_pct == 80


def test_missing_required_field(tmp_path):
    text = MINIMAL.replace("  tmdb: {token: m}\n", "")
    with pytest.raises(ConfigError, match="Missing settings: TMDB read access token"):
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


@pytest.mark.parametrize("blank", ["", "  "])
def test_blank_env_value_does_not_override(tmp_path, blank):
    config = load_config(
        write(tmp_path, MINIMAL), environ={"HANDPICKED_TMDB_TOKEN": blank}
    )
    assert config.services.tmdb.token == "m"


ENVIRON = {
    "HANDPICKED_TAUTULLI_URL": "http://tautulli:8181",
    "HANDPICKED_TAUTULLI_API_KEY": "t",
    "HANDPICKED_SEERR_URL": "http://seerr:5055",
    "HANDPICKED_SEERR_API_KEY": "s",
    "HANDPICKED_TMDB_TOKEN": "m",
}


def test_environment_alone_is_a_config(tmp_path):
    environ = ENVIRON | {
        "HANDPICKED_SCHEDULE": "30 6 * * MON-FRI",
        "HANDPICKED_PICKS_PER_RUN": "5",
        "HANDPICKED_SMTP_HOST": "smtp.example.com",
        "HANDPICKED_SMTP_USERNAME": "handpicked@example.com",
        "HANDPICKED_SMTP_PASSWORD": "p",
        "HANDPICKED_DIGEST_RECIPIENTS": "admin@example.com, other@example.com,",
    }

    config = load_config(tmp_path / "config.yml", environ=environ)

    assert config.services.tautulli.url == "http://tautulli:8181"
    assert config.services.seerr.url == "http://seerr:5055"
    assert config.schedule == "30 6 * * MON-FRI"
    assert config.picks_per_run == 5
    assert config.dry_run is False
    digest = config.digest
    assert (digest.host, digest.security, digest.port) == (
        "smtp.example.com",
        "starttls",
        None,
    )
    assert digest.sender == "handpicked@example.com"
    assert digest.recipients == ["admin@example.com", "other@example.com"]


def test_blank_template_fields_leave_out_the_digest(tmp_path):
    blank = {var: "" for var in ("HANDPICKED_SMTP_HOST", "HANDPICKED_SMTP_PASSWORD")}

    config = load_config(tmp_path / "config.yml", environ=ENVIRON | blank)

    assert config.digest is None


def test_env_overrides_file_settings(tmp_path):
    text = MINIMAL + 'schedule: "0 9 * * SUN"\n'
    environ = {
        "HANDPICKED_TAUTULLI_URL": "http://other:8181",
        "HANDPICKED_SCHEDULE": "0 7 * * SAT",
    }

    config = load_config(write(tmp_path, text), environ=environ)

    assert config.services.tautulli.url == "http://other:8181"
    assert config.schedule == "0 7 * * SAT"


def test_missing_settings_are_named_as_in_the_template(tmp_path):
    environ = {"HANDPICKED_TAUTULLI_URL": "http://tautulli:8181"}

    with pytest.raises(ConfigError) as raised:
        load_config(tmp_path / "config.yml", environ=environ)

    assert str(raised.value).startswith(
        "Missing settings: Tautulli API key, Seerr URL, Seerr API key,"
        " TMDB read access token. Fill them in on the container's template"
    )
    assert "(not found)" in str(raised.value)


def test_a_misspelled_key_is_named_with_the_missing_setting(tmp_path):
    text = MINIMAL.replace("{token: m}", "{tokn: m}")

    with pytest.raises(ConfigError) as raised:
        load_config(write(tmp_path, text), environ={})

    assert str(raised.value).startswith("Missing settings: TMDB read access token.")
    assert "services.tmdb.tokn: Extra inputs are not permitted" in str(raised.value)


def test_a_missing_setting_does_not_hide_other_errors(tmp_path):
    text = MINIMAL.replace("  tmdb: {token: m}\n", "") + 'schedule: "0 9 * * 0"\n'

    with pytest.raises(ConfigError) as raised:
        load_config(write(tmp_path, text), environ={})

    assert str(raised.value).startswith("Missing settings: TMDB read access token.")
    assert "schedule: Value error, name the day of the week" in str(raised.value)


@pytest.mark.parametrize(
    ("text", "error"),
    [
        (
            MINIMAL.replace("{url: http://tautulli:8181, api_key: t}", "http://t:8181"),
            "services.tautulli: Input should be a valid dictionary",
        ),
        (
            MINIMAL.replace("api_key: t}", "api_key: 0}"),
            "services.tautulli.api_key: Input should be a valid string",
        ),
        ("services: [a]\n", "services: Input should be a valid dictionary"),
    ],
)
def test_a_wrong_type_is_not_called_missing(tmp_path, text, error):
    with pytest.raises(ConfigError) as raised:
        load_config(write(tmp_path, text), environ={})

    assert "Missing settings" not in str(raised.value)
    assert error in str(raised.value)


def test_an_invalid_value_is_not_echoed(tmp_path):
    text = MINIMAL.replace("api_key: s}", "api_key: [s3cret]}")

    with pytest.raises(ConfigError) as raised:
        load_config(write(tmp_path, text), environ={})

    assert "services.seerr.api_key" in str(raised.value)
    assert "s3cret" not in str(raised.value)
    assert raised.value.__cause__ is None and raised.value.__suppress_context__


@pytest.mark.parametrize(
    "blank", ['api_key: ""', 'api_key: "  "', "api_key:", "api_key: null"]
)
def test_a_blank_setting_is_missing(tmp_path, blank):
    text = MINIMAL.replace("tautulli: {url: http://tautulli:8181, api_key: t}", "")
    text = text.replace(
        "services:\n", f"services:\n  tautulli:\n    url: u\n    {blank}\n"
    )

    with pytest.raises(ConfigError, match="^Missing settings: Tautulli API key"):
        load_config(write(tmp_path, text), environ={})


def test_a_blank_section_is_missing(tmp_path):
    text = MINIMAL.replace("{url: http://tautulli:8181, api_key: t}", "")

    with pytest.raises(ConfigError, match="^Missing settings: Tautulli URL, Tautulli"):
        load_config(write(tmp_path, text), environ={})


def test_a_partial_digest_names_what_it_still_needs(tmp_path):
    environ = ENVIRON | {"HANDPICKED_SMTP_PASSWORD": "p"}

    with pytest.raises(ConfigError, match="Missing settings: SMTP host, Digest"):
        load_config(tmp_path / "config.yml", environ=environ)


def test_digest_needs_a_sender_or_username(tmp_path):
    text = MINIMAL + "digest: {host: smtp.example.com, recipients: [a@example.com]}\n"

    with pytest.raises(ConfigError, match="set a sender, or a username"):
        load_config(write(tmp_path, text), environ={})


def test_unreadable_file(tmp_path):
    with pytest.raises(ConfigError, match="Cannot read"):
        load_config(tmp_path, environ={})


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
