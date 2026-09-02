"""Spec section 4.3."""

from __future__ import annotations

from datetime import time
from pathlib import Path

import pytest

from chase.config import ConfigError, load_client


def test_both_clients_load(config_dir):
    for name in ("harbourline", "lakeshore"):
        client = load_client(name, config_dir)
        assert client.client == name
        assert client.ladder
        assert client.work_categories


def test_config_loads_both_yamls_and_they_differ_in_policy(config_dir):
    """The second client exists to prove onboarding is a file, so it has to differ in
    policy rather than in three integers. Spec section 4.3."""
    h = load_client("harbourline", config_dir)
    l = load_client("lakeshore", config_dir)

    assert h.send_window.days != l.send_window.days           # five day week vs six
    assert h.send_window.tz != l.send_window.tz               # two DST regimes
    assert len(h.ladder) != len(l.ladder)                     # three steps vs four
    assert [s.days_after_sent for s in h.ladder] != [s.days_after_sent for s in l.ladder]
    assert h.tone != l.tone
    assert h.digest.stale_after_days != l.digest.stale_after_days
    assert h.work_categories != l.work_categories
    assert h.currency != l.currency


def test_harbourline_shape(config_dir):
    h = load_client("harbourline", config_dir)
    assert [s.days_after_sent for s in h.ladder] == [3, 7, 14]
    assert h.send_window.days == ("mon", "tue", "wed", "thu", "fri")
    assert h.send_window.start == time(8, 0) and h.send_window.end == time(18, 0)
    assert h.send_window.tz == "Australia/Sydney"
    assert h.last_step == 3
    assert h.stop_on_stages == ("Won", "Lost")


def test_lakeshore_shape(config_dir):
    l = load_client("lakeshore", config_dir)
    assert [s.days_after_sent for s in l.ladder] == [2, 5, 10, 20]
    assert "sat" in l.send_window.days
    assert l.send_window.tz == "America/Chicago"
    assert l.last_step == 4


def test_only_step_one_uses_the_model(config_dir):
    """Spec section 2: no AI beyond one bounded opening line on the first chase."""
    for name in ("harbourline", "lakeshore"):
        client = load_client(name, config_dir)
        assert client.ladder[0].opener == "groq"
        assert all(s.opener == "template" for s in client.ladder[1:])


def test_every_template_file_exists(config_dir):
    root = Path(config_dir).parent
    for name in ("harbourline", "lakeshore"):
        for step in load_client(name, config_dir).ladder:
            assert (root / step.template).exists(), f"missing {step.template}"


def test_stop_on_stages_never_lists_a_non_candidate_stage(config_dir):
    """Replied is not a candidate stage for either client, so listing it in stop_on
    would be a policy difference nobody could observe. Spec section 4.3."""
    for name in ("harbourline", "lakeshore"):
        assert "Replied" not in load_client(name, config_dir).stop_on_stages


def test_missing_file_is_a_clear_error(tmp_path):
    with pytest.raises(ConfigError, match="no client file"):
        load_client("nobody", tmp_path)


@pytest.mark.parametrize(
    "mutation, message",
    [
        ("del d['tone']", "tone"),
        ("d['ladder'][0]['opener'] = 'magic'", "opener"),
        ("d['send_window']['days'] = ['funday']", "unknown day"),
        ("d['send_window']['start'] = '18:00'", "not before"),
        ("d['ladder'] = []", "ladder is empty"),
        ("d['work_categories'] = []", "work_categories is empty"),
        ("d['ladder'][1]['days_after_sent'] = 1", "must increase"),
    ],
)
def test_bad_config_fails_at_load_not_at_run(tmp_path, config_dir, mutation, message):
    """A malformed client must fail on load. Discovering it mid-run means some quotes
    were already chased under a half-valid config."""
    import yaml

    d = yaml.safe_load((Path(config_dir) / "harbourline.yaml").read_text(encoding="utf-8"))
    exec(mutation, {"d": d})
    (tmp_path / "broken.yaml").write_text(yaml.safe_dump(d), encoding="utf-8")
    with pytest.raises(ConfigError, match=message):
        load_client("broken", tmp_path)
