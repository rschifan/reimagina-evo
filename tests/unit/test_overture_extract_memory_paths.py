"""The two extract paths, and the contract that they are interchangeable.

`low_memory` is a performance choice, not a semantic one: both paths must
return the same rows. Measured on the Mexicali bbox, release 2026-10-21.0,
512 files, 387,273 matching rows:

    default      4,457 MB    40 s
    low_memory     663 MB    454 s

⚠ The 4.4 GB is parquet metadata, not row data. Attributed by measurement:

    baseline interpreter               109 MB
    after pads.dataset()               132 MB   (1 s)
    after count_rows(filter)         4,170 MB   (139 s)  <-- here
    after draining 387,273 rows      4,578 MB   (4 s)

Two cheaper fixes were tried first and are recorded because they look
obviously right and are not: batching the write instead of one table
(4,457 -> 4,304 MB) and capping fragment/batch readahead (4,285 MB at 1/2).
Neither touches metadata, so neither helps.

These tests do not hit S3 — they pin the wiring and the documented numbers, so
a later edit cannot quietly drop the flag or reverse which path is which.
"""

from __future__ import annotations

import inspect

from reimagina.pipeline import overture


def test_both_paths_are_reachable_from_the_public_entry_point():
    sig = inspect.signature(overture.extract_overture_buildings_subset)
    assert "low_memory" in sig.parameters
    assert sig.parameters["low_memory"].default is False, (
        "the fast path stays the default: 4.4 GB is affordable on a server and "
        "454 s is not free"
    )


def test_low_memory_is_threaded_to_the_remote_extractor():
    # The local-GeoParquet path has no S3 metadata to bound, so only the
    # remote one takes the flag.
    source = inspect.getsource(overture.extract_overture_buildings_subset)
    assert "low_memory=low_memory" in source


def test_the_remote_extractor_branches_on_it():
    source = inspect.getsource(overture._extract_remote_overture_subset)
    assert "if low_memory:" in source
    assert "_write_subset_per_file" in source
    # The fast path must stay a single materialised write — batching it was
    # measured and bought 150 MB for 29 s.
    assert "pq.write_table(dataset.to_table(filter=flt)" in source


def test_the_per_file_path_drops_each_datasets_metadata():
    """The `del` is the mechanism, not tidiness.

    Holding one file's metadata at a time is what makes the ceiling flat;
    keeping the dataset objects alive would reproduce the 4 GB one file at a
    time.
    """
    source = inspect.getsource(overture._write_subset_per_file)
    assert "del one" in source
    assert "pads.dataset(path" in source


def test_an_empty_result_still_writes_a_file():
    """Both paths, because the caller stages the path into Bronze.

    A missing file fails with "no such file" instead of "the bbox is empty",
    which sends the reader to the wrong problem.
    """
    source = inspect.getsource(overture._write_subset_per_file)
    assert "empty_table()" in source


def test_the_batch_size_is_a_named_constant():
    assert isinstance(overture._EXTRACT_BATCH_ROWS, int)
    assert overture._EXTRACT_BATCH_ROWS > 0


def test_the_measurement_is_recorded_where_the_choice_is_made():
    """A performance flag with no numbers beside it is a coin toss.

    Pinned because the whole justification for `low_memory` existing — and for
    the default being off — is a measurement someone has to be able to find.
    """
    doc = overture.extract_overture_buildings_subset.__doc__ or ""
    assert "663 MB" in doc and "4,457 MB" in doc
    assert "454 s" in doc and "40 s" in doc

    attribution = overture._write_subset_per_file.__doc__ or ""
    assert "metadata, not data" in attribution
    assert "4,170 MB" in attribution, "the line that locates the cost"
