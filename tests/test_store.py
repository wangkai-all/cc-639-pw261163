"""Local store tests: dedup, conflict, quarantine."""

from pydicom.dataset import FileMetaDataset
from pydicom.uid import ExplicitVRLittleEndian

from server.demo_data import BADMETA_WRONG_UID, CT1_UID, build_demo_archive


def _ct1():
    archive = build_demo_archive()
    return next(
        i.dataset for i in archive
        if str(i.dataset.SOPInstanceUID) == str(CT1_UID) and "private-tags" in i.notes
    )


def _with_meta(ds, sop_uid=None, sop_class=None):
    ds.file_meta = FileMetaDataset()
    ds.file_meta.MediaStorageSOPClassUID = sop_class or ds.SOPClassUID
    ds.file_meta.MediaStorageSOPInstanceUID = sop_uid or ds.SOPInstanceUID
    ds.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    return ds


def test_store_new_instance(store):
    ds = _with_meta(_ct1())
    res = store.receive_instance(ds, source_ae="TEST")
    assert res["status"] == "stored"
    entry = store.get_entry(str(CT1_UID))
    assert entry["status"] == "stored"
    assert entry["modality"] == "CT"


def test_duplicate_same_content(store):
    res1 = store.receive_instance(_with_meta(_ct1()), source_ae="TEST")
    res2 = store.receive_instance(_with_meta(_ct1()), source_ae="TEST")
    assert res1["status"] == "stored"
    assert res2["status"] == "duplicate"
    entry = store.get_entry(str(CT1_UID))
    assert entry["status"] == "stored"
    assert len(entry["versions"]) == 1


def test_conflict_same_uid_different_content(store):
    first = _with_meta(_ct1())
    store.receive_instance(first, source_ae="TEST")

    second = _ct1()
    second.PatientName = "PHANTOM^DIFFERENT"
    second.InstitutionName = "Changed"
    _with_meta(second)
    res = store.receive_instance(second, source_ae="TEST")

    assert res["status"] == "conflict"
    entry = store.get_entry(str(CT1_UID))
    assert entry["status"] == "conflict"
    assert len(entry["versions"]) == 2
    # original file untouched: active version is still the first content
    ds = store.load_dataset(str(CT1_UID))
    assert str(ds.PatientName) == "PHANTOM^HEAD^A"
    # conflict diff recorded
    assert "PatientName" in entry["conflict"]


def test_quarantine_on_meta_dataset_mismatch(store):
    ds = _ct1()
    _with_meta(ds, sop_uid=BADMETA_WRONG_UID)  # file meta disagrees with dataset
    res = store.receive_instance(ds, source_ae="TEST")
    assert res["status"] == "quarantined"
    assert "MediaStorageSOPInstanceUID" in res["detail"]
    entry = store.get_entry(str(CT1_UID))
    assert entry["status"] == "quarantined"
    quarantined = store.quarantined()
    assert len(quarantined) == 1
    # quarantined instances stay out of the hierarchy
    assert store.hierarchy() == []


def test_hierarchy_levels(store):
    archive = build_demo_archive()
    for inst in archive[:3]:  # series 1: three CT instances
        store.receive_instance(_with_meta(inst.dataset), source_ae="TEST")
    tree = store.hierarchy()
    assert len(tree) == 1
    assert len(tree[0]["series"]) == 1
    assert len(tree[0]["series"][0]["instances"]) == 3
