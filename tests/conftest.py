import os
from datetime import date
from pathlib import Path

import pytest

os.environ["LESSONBRIDGE_ALLOW_NETWORK"] = "0"
os.environ["LESSONBRIDGE_GENERATOR"] = "template"

from lessonbridge import db  # noqa: E402
from lessonbridge.config import Settings  # noqa: E402

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


@pytest.fixture()
def cfg(tmp_path):
    s = Settings(data_dir=tmp_path / "data", allow_network=False, generator="template")
    db.init_engine(s)
    yield s


@pytest.fixture()
def onboarded(cfg):
    """A fully onboarded sample teacher; returns (cfg, teacher_id)."""
    from lessonbridge.profile.onboarding import OnboardingInput, run_onboarding

    inp = OnboardingInput.from_yaml(EXAMPLES / "teacher_profile.yaml")
    with db.session_scope() as s:
        res = run_onboarding(s, inp, cfg=cfg, today=date(2026, 10, 5))
        tid = res.teacher_id
    return cfg, tid
