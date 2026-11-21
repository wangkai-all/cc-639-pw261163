"""Anonymization rule tests."""

from server.anonymize import (
    AnonymizeStats,
    UIDMapper,
    anonymize_dataset,
    rules_document,
)
from server.demo_data import (
    CT1_UID,
    CT3_UID,
    FOR_UID,
    SERIES1_UID,
    STUDY1_UID,
    build_demo_archive,
)


def _fresh(uid):
    archive = build_demo_archive()
    return next(i.dataset for i in archive if str(i.dataset.SOPInstanceUID) == str(uid))


def test_identity_fields_removed_and_acquisition_kept():
    ds = _fresh(CT1_UID)
    anonymize_dataset(ds, UIDMapper("salt"))
    for kw in ("PatientName", "PatientID", "PatientBirthDate", "AccessionNumber",
               "ReferringPhysicianName", "InstitutionName", "StationName",
               "StudyDate", "StudyTime", "OperatorsName"):
        assert kw not in ds, kw
    for kw in ("Modality", "Rows", "Columns", "KVP", "PixelSpacing",
               "SliceThickness", "WindowCenter", "ImagePositionPatient"):
        assert kw in ds, kw
    assert ds.KVP == 120


def test_private_tags_removed_by_default():
    ds = _fresh(CT1_UID)
    assert any(t.is_private for t in ds.keys())
    stats = AnonymizeStats()
    anonymize_dataset(ds, UIDMapper("salt"), stats)
    assert not any(t.is_private for t in ds.keys())
    assert stats.removed_private_tags == 3


def test_uid_remap_consistent_across_instances_and_nested_sequences():
    ct1 = _fresh(CT1_UID)
    ct3 = _fresh(CT3_UID)  # derived; SourceImageSequence references ct1/ct2
    mapper = UIDMapper("salt")
    anonymize_dataset(ct1, mapper)
    anonymize_dataset(ct3, mapper)

    # identity UIDs remapped
    assert str(ct1.StudyInstanceUID) != str(STUDY1_UID)
    assert str(ct1.SeriesInstanceUID) != str(SERIES1_UID)
    assert str(ct1.SOPInstanceUID) != str(CT1_UID)
    assert str(ct1.FrameOfReferenceUID) != str(FOR_UID)
    # same original -> same mapped value across instances
    assert ct3.StudyInstanceUID == ct1.StudyInstanceUID
    assert ct3.FrameOfReferenceUID == ct1.FrameOfReferenceUID
    # nested sequence reference points at the mapped UID of ct1
    ref = ct3.SourceImageSequence[0]
    assert str(ref.ReferencedSOPInstanceUID) == str(ct1.SOPInstanceUID)
    # file meta synced to the dataset mapping
    assert str(ct1.file_meta.MediaStorageSOPInstanceUID) == str(ct1.SOPInstanceUID)


def test_type_identifiers_preserved():
    ds = _fresh(CT3_UID)
    anonymize_dataset(ds, UIDMapper("salt"))
    assert str(ds.SOPClassUID) == "1.2.840.10008.5.1.4.1.1.2"
    assert str(ds.file_meta.MediaStorageSOPClassUID) == "1.2.840.10008.5.1.4.1.1.2"
    # Transfer Syntax UID untouched (no transcoding anywhere)
    assert str(ds.file_meta.TransferSyntaxUID) == "1.2.840.10008.1.2.1"
    # Referenced SOP Class UID inside nested sequence preserved
    assert str(ds.SourceImageSequence[0].ReferencedSOPClassUID) == "1.2.840.10008.5.1.4.1.1.2"


def test_mapping_is_per_run():
    a = UIDMapper("run-a")
    b = UIDMapper("run-b")
    assert a.map(str(STUDY1_UID)) != b.map(str(STUDY1_UID))
    assert a.map(str(STUDY1_UID)) == a.map(str(STUDY1_UID))
    assert a.map(str(STUDY1_UID)).startswith("2.25.")


def test_rules_document_shape():
    doc = rules_document()
    assert doc["remove_keywords"]
    assert doc["keep_acquisition_keywords"]
    assert doc["uid_remap"]["remapped_keywords"]
    assert "TransferSyntaxUID" in doc["uid_remap"]["never_remapped_keywords"]
    assert doc["private_tags"]["default"] == "remove"
