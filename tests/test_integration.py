"""End-to-end DICOM networking test: C-FIND + C-MOVE against the demo
service, all in-process on test ports."""

from server.demo_data import (
    SERIES1_UID,
    SERIES5_UID,
    SERIES6_UID,
    STUDY1_UID,
    STUDY2_UID,
)


def test_find_studies(dicom_nodes):
    _, recv = dicom_nodes
    studies = recv.find_studies()
    assert len(studies) == 2
    descriptions = {s["StudyDescription"] for s in studies}
    assert "合成体模研究一" in descriptions


def test_move_series_stores_instances(dicom_nodes):
    store, recv = dicom_nodes
    res = recv.cmove_series(str(STUDY1_UID), str(SERIES1_UID))
    assert res["final_status"] == "0x0000"
    assert res["suboperations"]["completed"] == 3
    tree = store.hierarchy()
    assert len(tree) == 1
    assert len(tree[0]["series"]) == 1
    assert len(tree[0]["series"][0]["instances"]) == 3


def test_move_meta_mismatch_series_is_quarantined(dicom_nodes):
    store, recv = dicom_nodes
    res = recv.cmove_series(str(STUDY2_UID), str(SERIES5_UID))
    assert res["final_status"] == "0x0000"
    quarantined = store.quarantined()
    assert len(quarantined) == 1
    mismatch = quarantined[0]["meta_mismatch"]
    assert "MediaStorageSOPInstanceUID" in mismatch
    assert store.hierarchy() == []


def test_move_conflicting_series_flags_conflict(dicom_nodes):
    store, recv = dicom_nodes
    recv.cmove_series(str(STUDY1_UID), str(SERIES1_UID))
    res = recv.cmove_series(str(STUDY1_UID), str(SERIES6_UID))
    assert res["final_status"] == "0x0000"
    conflicts = store.conflicts()
    assert len(conflicts) == 1
    assert len(conflicts[0]["versions"]) == 2
