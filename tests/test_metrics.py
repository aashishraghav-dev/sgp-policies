"""Tests for the run-metrics recorder.

The governing constraint is that metrics are diagnostics, not the product:
a broken recorder must never take a scrape down with it. Most of what is
asserted here is that failure modes stay contained.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path

import pytest

from policy_scraper.core.metrics import (
    NULL_RECORDER,
    SCHEMA_VERSION,
    MetricsRecorder,
    NullRecorder,
)


def read_events(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


@pytest.fixture
def recorder(tmp_path: Path) -> MetricsRecorder:
    return MetricsRecorder(tmp_path / "run.jsonl", run_id="test123")


class TestWriting:
    def test_each_event_is_one_json_line(self, recorder: MetricsRecorder) -> None:
        recorder.emit("fetch", url="https://example.test/a")
        recorder.emit("fetch", url="https://example.test/b")
        assert len(read_events(recorder.path)) == 2

    def test_every_event_carries_run_identity_and_schema(
        self, recorder: MetricsRecorder
    ) -> None:
        recorder.emit("run_start")
        event = read_events(recorder.path)[0]
        assert event["run_id"] == "test123"
        assert event["schema_version"] == SCHEMA_VERSION
        assert event["event"] == "run_start"
        assert event["ts"].endswith("+00:00")

    def test_fields_are_preserved_verbatim(self, recorder: MetricsRecorder) -> None:
        recorder.emit("document", stages={"fetch": 0.2, "convert": 80.1}, pages=2)
        event = read_events(recorder.path)[0]
        assert event["stages"] == {"fetch": 0.2, "convert": 80.1}
        assert event["pages"] == 2

    def test_unserialisable_values_do_not_lose_the_event(
        self, recorder: MetricsRecorder
    ) -> None:
        # A stray enum or Path in a field should degrade to a string, not
        # throw away the whole record.
        recorder.emit("store", path=Path("/tmp/x"), when=object())
        event = read_events(recorder.path)[0]
        assert event["path"] == "/tmp/x"

    def test_a_run_id_is_generated_when_not_supplied(self, tmp_path: Path) -> None:
        assert MetricsRecorder(tmp_path / "r.jsonl").run_id != ""

    def test_missing_parent_directories_are_created(self, tmp_path: Path) -> None:
        recorder = MetricsRecorder(tmp_path / "deep" / "nested" / "run.jsonl")
        recorder.emit("run_start")
        assert recorder.path.exists()


class TestPathTemplating:
    def test_run_id_is_interpolated_into_the_filename(self, tmp_path: Path) -> None:
        recorder = MetricsRecorder(tmp_path / "run-{run_id}.jsonl", run_id="abc")
        assert recorder.path.name == "run-abc.jsonl"

    def test_date_is_interpolated(self, tmp_path: Path) -> None:
        recorder = MetricsRecorder(tmp_path / "{date}.jsonl")
        assert recorder.path.name.count("-") == 2  # YYYY-MM-DD

    def test_a_fixed_name_appends_across_runs(self, tmp_path: Path) -> None:
        path = tmp_path / "all.jsonl"
        MetricsRecorder(path, run_id="one").emit("run_start")
        MetricsRecorder(path, run_id="two").emit("run_start")
        assert {e["run_id"] for e in read_events(path)} == {"one", "two"}


class TestTimed:
    def test_a_successful_block_records_ok_and_a_duration(
        self, recorder: MetricsRecorder
    ) -> None:
        with recorder.timed("convert", converter="docling_local"):
            pass
        event = read_events(recorder.path)[0]
        assert event["ok"] is True
        assert event["converter"] == "docling_local"
        assert isinstance(event["seconds"], float)

    def test_the_yielded_dict_reaches_the_event(self, recorder: MetricsRecorder) -> None:
        # Results only known after the work: byte counts, page counts.
        with recorder.timed("fetch") as extra:
            extra["bytes"] = 4096
        assert read_events(recorder.path)[0]["bytes"] == 4096

    def test_a_failure_is_recorded_and_then_re_raised(
        self, recorder: MetricsRecorder
    ) -> None:
        with pytest.raises(ValueError, match="boom"):
            with recorder.timed("fetch", url="https://example.test"):
                raise ValueError("boom")

        event = read_events(recorder.path)[0]
        assert event["ok"] is False
        assert event["error_type"] == "ValueError"
        assert "boom" in event["error"]
        assert event["url"] == "https://example.test"

    def test_a_slow_failure_still_reports_its_duration(
        self, recorder: MetricsRecorder
    ) -> None:
        # A stage that is slow *because* it keeps failing should be visible
        # as slow, not excluded from the timings.
        with pytest.raises(RuntimeError):
            with recorder.timed("fetch"):
                raise RuntimeError("timeout")
        assert read_events(recorder.path)[0]["seconds"] >= 0


class TestResilience:
    """A metrics failure must never break a scrape."""

    def test_a_write_error_disables_recording_instead_of_raising(
        self, recorder: MetricsRecorder
    ) -> None:
        recorder._handle.close()  # simulate a full disk / closed handle
        recorder.emit("fetch")  # must not raise
        recorder.emit("fetch")
        assert recorder._broken is True

    def test_timing_still_yields_when_recording_is_broken(
        self, recorder: MetricsRecorder
    ) -> None:
        recorder._handle.close()
        entered = False
        with recorder.timed("convert"):
            entered = True
        assert entered

    def test_closing_twice_is_safe(self, recorder: MetricsRecorder) -> None:
        recorder.close()
        recorder.close()


class TestConcurrency:
    def test_concurrent_emits_produce_intact_lines(self, tmp_path: Path) -> None:
        # Documents within a category run on a thread pool, so interleaved
        # writes must not corrupt each other into unparseable lines.
        recorder = MetricsRecorder(tmp_path / "run.jsonl")

        def work(index: int) -> None:
            for _ in range(25):
                recorder.emit("fetch", worker=index, url="x" * 200)

        threads = [threading.Thread(target=work, args=(i,)) for i in range(8)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        recorder.close()

        events = read_events(recorder.path)  # raises if any line is torn
        assert len(events) == 200


class TestNullRecorder:
    def test_it_writes_nothing_and_needs_no_file(self) -> None:
        NULL_RECORDER.emit("run_start", anything=1)
        NULL_RECORDER.close()

    def test_timed_still_yields_a_usable_dict(self) -> None:
        with NULL_RECORDER.timed("fetch") as extra:
            extra["bytes"] = 1
        assert extra == {"bytes": 1}

    def test_a_failure_inside_a_null_timed_block_still_propagates(self) -> None:
        with pytest.raises(ValueError):
            with NULL_RECORDER.timed("fetch"):
                raise ValueError("boom")

    def test_it_satisfies_the_recorder_interface(self) -> None:
        # Callers type against MetricsRecorder and never check for None.
        assert isinstance(NULL_RECORDER, MetricsRecorder)
        assert isinstance(NULL_RECORDER, NullRecorder)
