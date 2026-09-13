"""Ingest tests (design.md section 3.1, DEC-003)."""
import pytest

import ingest
from ingest import IngestError, image_path, load_all

DATASET = ingest.Path(__file__).resolve().parent.parent.parent / "dataset"


def test_load_all_on_real_dataset():
    ds = load_all(DATASET)
    assert len(ds.profiles) == 275
    assert len(ds.events) >= 25000
    assert len(ds.requests) == 250
    assert sum(len(v) for v in ds.payment_options.values()) == 790
    assert len(ds.payment_options) >= len(ds.requests)
    assert len(ds.messages) == 215
    assert len(ds.images) == 16
    assert len(ds.exchange_rates) == 5


def test_blank_amount_events_flagged_not_zeroed():
    ds = load_all(DATASET)
    blank = ds.blank_amount_events
    assert len(blank) == 16
    assert all(e.amount is None for e in blank)
    for e in blank:
        assert any(i.related_event_id == e.event_id for i in ds.images)


def test_image_path_roundtrip():
    ds = load_all(DATASET)
    p = image_path(ds, "image_01")
    assert p.name == "image_01.png" and p.exists()


def test_schema_failure_raises():
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as td:
        bad = Path(td)
        (bad / "requests.csv").write_text("request_id,user_id\nr1,u1\n", encoding="utf-8")
        with pytest.raises(IngestError):
            load_all(bad)