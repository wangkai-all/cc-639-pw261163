"""Reference extraction and integrity report tests."""

from server.demo_data import (
    CT1_UID,
    CT2_UID,
    KOS1_UID,
    MISSING_REF_UID,
    build_demo_archive,
)
from server.references import build_integrity_report, extract_references


def _kos():
    archive = build_demo_archive()
    return next(i.dataset for i in archive if str(i.dataset.SOPInstanceUID) == str(KOS1_UID))


def test_extract_nested_references():
    refs = extract_references(_kos())
    targets = {r["referenced_sop_instance_uid"] for r in refs}
    assert str(CT1_UID) in targets
    assert str(CT2_UID) in targets
    assert str(MISSING_REF_UID) in targets
    # nesting path recorded
    deep = next(r for r in refs if r["referenced_sop_instance_uid"] == str(CT1_UID))
    assert "ReferencedStudySequence" in deep["path"]
    assert "ReferencedSeriesSequence" in deep["path"]
    assert "ReferencedSOPSequence" in deep["path"]
    assert deep["referenced_sop_class_uid"] == "1.2.840.10008.5.1.4.1.1.2"


def test_integrity_report_flags_missing(store):
    from pydicom.dataset import FileMetaDataset
    from pydicom.uid import ExplicitVRLittleEndian

    kos = _kos()
    kos.file_meta = FileMetaDataset()
    kos.file_meta.MediaStorageSOPClassUID = kos.SOPClassUID
    kos.file_meta.MediaStorageSOPInstanceUID = kos.SOPInstanceUID
    kos.file_meta.TransferSyntaxUID = ExplicitVRLittleEndian
    store.receive_instance(kos, source_ae="TEST")

    report = build_integrity_report(store)
    assert report["summary"]["missing_references"] == 3  # all three targets absent
    missing_uids = {m["missing_sop_instance_uid"] for m in report["missing"]}
    assert str(MISSING_REF_UID) in missing_uids
