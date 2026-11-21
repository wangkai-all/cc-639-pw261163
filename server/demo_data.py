"""Programmatic generator for the synthetic demo archive.

Every dataset here is fabricated: phantoms, not patients. UIDs are
deterministic (uuid5 in the 2.25 arc) so the archive is identical on every
run, which keeps tests and demos reproducible.

The archive deliberately contains the edge cases the workstation must
handle:

* nested references (ReferencedStudySequence > ReferencedSeriesSequence >
  ReferencedSOPSequence, plus SourceImageSequence on a derived image)
* a reference to an instance that does not exist in the archive
* three transfer syntaxes: Explicit VR LE, Implicit VR LE, Deflated Explicit
  VR LE (pydicom-native, no transcoding anywhere)
* an identifier conflict: the same SOP Instance UID with different content
  (served from a separate series so both copies can be retrieved)
* an instance whose C-STORE command-set Affected SOP Instance UID will not
  match the dataset SOP Instance UID (file-meta/dataset mismatch once the
  receiver reconstructs file meta) -> quarantine
* private tags, to demonstrate the default private-tag rule
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

import numpy as np
from pydicom.dataset import Dataset, FileMetaDataset
from pydicom.uid import (
    UID,
    CTImageStorage,
    MRImageStorage,
    KeyObjectSelectionDocumentStorage,
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    DeflatedExplicitVRLittleEndian,
    generate_uid,
)

_UID_NS = "dicom-web-workstation.demo."


def demo_uid(name: str) -> UID:
    """Deterministic UID in the 2.25 (UUID) arc for a logical demo name."""
    return UID(f"2.25.{uuid.uuid5(uuid.NAMESPACE_DNS, _UID_NS + name).int}")


# --- Logical UIDs of the archive -------------------------------------------
STUDY1_UID = demo_uid("study.1")
STUDY2_UID = demo_uid("study.2")
SERIES1_UID = demo_uid("study.1.series.1")   # CT, Explicit VR LE
SERIES2_UID = demo_uid("study.1.series.2")   # CT, Implicit VR LE
SERIES3_UID = demo_uid("study.1.series.3")   # Key Object Selection (references)
SERIES6_UID = demo_uid("study.1.series.6")   # conflict demo series
SERIES4_UID = demo_uid("study.2.series.4")   # MR, Deflated Explicit VR LE
SERIES5_UID = demo_uid("study.2.series.5")   # meta/dataset mismatch demo
FOR_UID = demo_uid("frame-of-reference.1")   # shared by series 1, 2 and 4

CT1_UID = demo_uid("study.1.series.1.instance.1")
CT2_UID = demo_uid("study.1.series.1.instance.2")
CT3_UID = demo_uid("study.1.series.1.instance.3")
CT4_UID = demo_uid("study.1.series.2.instance.4")
CT5_UID = demo_uid("study.1.series.2.instance.5")
KOS1_UID = demo_uid("study.1.series.3.instance.1")
MR1_UID = demo_uid("study.2.series.4.instance.1")
MR2_UID = demo_uid("study.2.series.4.instance.2")
BADMETA_UID = demo_uid("study.2.series.5.instance.1")
#: Referenced from the KOS instance but absent from the archive.
MISSING_REF_UID = demo_uid("referenced.but.missing.instance")
#: The wrong UID the demo service puts in the C-STORE command set for the
#: mismatch instance (receiver reconstructs file meta from it -> quarantine).
BADMETA_WRONG_UID = demo_uid("study.2.series.5.instance.1.wrong-meta")


@dataclass
class DemoInstance:
    """One archived instance plus serving hints for the demo service."""

    dataset: Dataset
    #: If set, the C-STORE command set uses this Affected SOP Instance UID
    #: instead of the dataset's own (meta/dataset mismatch scenario).
    command_set_uid: UID | None = None
    notes: list[str] = field(default_factory=list)


def _file_meta(sop_class: UID, sop_instance: UID, tsyntax: UID) -> FileMetaDataset:
    meta = FileMetaDataset()
    meta.MediaStorageSOPClassUID = sop_class
    meta.MediaStorageSOPInstanceUID = sop_instance
    meta.TransferSyntaxUID = tsyntax
    meta.ImplementationClassUID = generate_uid()
    meta.ImplementationVersionName = "DEMOSCP_1"
    return meta


def _phantom_pixels(seed: int, kind: str) -> bytes:
    """64x64 uint16 synthetic phantom slice. Deterministic per seed."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:64, 0:64]
    img = np.zeros((64, 64), dtype=np.uint16)
    if kind == "ct":
        # raw values; RescaleIntercept -1000 makes background ~ -1000 HU
        body = ((yy - 32) ** 2 / 28.0**2 + (xx - 32) ** 2 / 20.0**2) <= 1.0
        insert = ((yy - 26) ** 2 + (xx - 38) ** 2) <= 6.0**2
        img[body] = 1000          # ~0 HU "water"
        img[insert] = 1800        # ~800 HU "dense insert"
        img += rng.integers(0, 40, size=img.shape, dtype=np.uint16)
    else:  # mr
        disc = ((yy - 32) ** 2 + (xx - 32) ** 2) <= 24.0**2
        img[disc] = 900
        img[((yy - 32) ** 2 + (xx - 20) ** 2) <= 8.0**2] = 1600
        img += (xx * 4).astype(np.uint16)  # gradient
        img += rng.integers(0, 60, size=img.shape, dtype=np.uint16)
    return img.tobytes()


def _base_image_ds(
    *,
    patient_name: str,
    patient_id: str,
    study_uid: UID,
    series_uid: UID,
    sop_uid: UID,
    sop_class: UID,
    tsyntax: UID,
    modality: str,
    seed: int,
    instance_number: int,
    series_number: int,
    study_description: str,
    series_description: str,
) -> Dataset:
    ds = Dataset()
    ds.file_meta = _file_meta(sop_class, sop_uid, tsyntax)

    ds.SpecificCharacterSet = "ISO_IR 192"
    ds.SOPClassUID = sop_class
    ds.SOPInstanceUID = sop_uid
    ds.StudyInstanceUID = study_uid
    ds.SeriesInstanceUID = series_uid
    ds.FrameOfReferenceUID = FOR_UID
    ds.Modality = modality
    ds.InstanceNumber = instance_number
    ds.SeriesNumber = series_number
    ds.ImageType = ["ORIGINAL", "PRIMARY", "AXIAL"]

    # Identity / demographics (synthetic; removed by the anonymizer).
    ds.PatientName = patient_name
    ds.PatientID = patient_id
    ds.PatientBirthDate = "19700101"
    ds.PatientSex = "O"
    ds.AccessionNumber = f"ACC{seed:08d}"
    ds.StudyID = f"STUDY{seed:05d}"
    ds.ReferringPhysicianName = "SYNTHETIC^REFERrer"
    ds.InstitutionName = "Synthetic Phantom Lab"
    ds.StationName = f"STATION-{seed:02d}"
    ds.OperatorsName = "AUTO^GENERATOR"
    ds.StudyDate = "20240115"
    ds.StudyTime = "120000"
    ds.SeriesDate = "20240115"
    ds.SeriesTime = "120100"
    ds.StudyDescription = study_description
    ds.SeriesDescription = series_description

    # Acquisition parameters (retained by the anonymizer).
    ds.Rows = 64
    ds.Columns = 64
    ds.BitsAllocated = 16
    ds.BitsStored = 16
    ds.HighBit = 15
    ds.PixelRepresentation = 0
    ds.SamplesPerPixel = 1
    ds.PhotometricInterpretation = "MONOCHROME2"
    ds.PixelSpacing = [0.5, 0.5]
    ds.SliceThickness = 2.5
    ds.ImagePositionPatient = [-16.0, -16.0, float(instance_number) * 2.5]
    ds.ImageOrientationPatient = [1, 0, 0, 0, 1, 0]
    ds.WindowCenter = 40 if modality == "CT" else 1000
    ds.WindowWidth = 400 if modality == "CT" else 2000
    if modality == "CT":
        ds.KVP = 120
        ds.XRayTubeCurrent = 200
        ds.ExposureTime = 500
        ds.RescaleIntercept = -1000
        ds.RescaleSlope = 1
        ds.RescaleType = "HU"
    else:
        ds.RepetitionTime = 500
        ds.EchoTime = 14
        ds.MagneticFieldStrength = 3.0
        ds.FlipAngle = 90

    ds.PixelData = _phantom_pixels(seed, "ct" if modality == "CT" else "mr")
    return ds


def _add_private_tags(ds: Dataset) -> None:
    """Private tags: the anonymizer's default rule must drop these."""
    block = ds.private_block(0x0011, "ACME PHANTOM", create=True)
    block.add_new(0x01, "LO", "ACME-INTERNAL-42")
    block.add_new(0x02, "US", 7)


def build_demo_archive() -> list[DemoInstance]:
    """Build the full synthetic archive (in memory)."""
    archive: list[DemoInstance] = []

    # --- Study 1 / Series 1: CT, Explicit VR LE ---------------------------
    ct1 = _base_image_ds(
        patient_name="PHANTOM^HEAD^A", patient_id="SYNTH-001",
        study_uid=STUDY1_UID, series_uid=SERIES1_UID, sop_uid=CT1_UID,
        sop_class=CTImageStorage, tsyntax=ExplicitVRLittleEndian,
        modality="CT", seed=11, instance_number=1, series_number=1,
        study_description="合成体模研究一", series_description="CT 平扫 第一组",
    )
    _add_private_tags(ct1)
    archive.append(DemoInstance(ct1, notes=["explicit-vr", "private-tags"]))

    ct2 = _base_image_ds(
        patient_name="PHANTOM^HEAD^A", patient_id="SYNTH-001",
        study_uid=STUDY1_UID, series_uid=SERIES1_UID, sop_uid=CT2_UID,
        sop_class=CTImageStorage, tsyntax=ExplicitVRLittleEndian,
        modality="CT", seed=12, instance_number=2, series_number=1,
        study_description="合成体模研究一", series_description="CT 平扫 第一组",
    )
    archive.append(DemoInstance(ct2, notes=["explicit-vr"]))

    # Derived image: nested SourceImageSequence referencing ct1/ct2.
    ct3 = _base_image_ds(
        patient_name="PHANTOM^HEAD^A", patient_id="SYNTH-001",
        study_uid=STUDY1_UID, series_uid=SERIES1_UID, sop_uid=CT3_UID,
        sop_class=CTImageStorage, tsyntax=ExplicitVRLittleEndian,
        modality="CT", seed=13, instance_number=3, series_number=1,
        study_description="合成体模研究一", series_description="CT 平扫 第一组",
    )
    ct3.ImageType = ["DERIVED", "SECONDARY"]
    ct3.DerivationDescription = "Synthetic average of instances 1 and 2"
    ct3.SourceImageSequence = []
    for ref_uid in (CT1_UID, CT2_UID):
        item = Dataset()
        item.ReferencedSOPClassUID = CTImageStorage
        item.ReferencedSOPInstanceUID = ref_uid
        ct3.SourceImageSequence.append(item)
    archive.append(DemoInstance(ct3, notes=["explicit-vr", "references"]))

    # --- Study 1 / Series 2: CT, Implicit VR LE ---------------------------
    for n, (uid, seed) in enumerate(((CT4_UID, 14), (CT5_UID, 15)), start=1):
        ds = _base_image_ds(
            patient_name="PHANTOM^HEAD^A", patient_id="SYNTH-001",
            study_uid=STUDY1_UID, series_uid=SERIES2_UID, sop_uid=uid,
            sop_class=CTImageStorage, tsyntax=ImplicitVRLittleEndian,
            modality="CT", seed=seed, instance_number=n, series_number=2,
            study_description="合成体模研究一", series_description="CT 平扫 第二组",
        )
        if n == 2:
            ds.BurnedInAnnotation = "YES"
        archive.append(DemoInstance(ds, notes=["implicit-vr"]))

    # --- Study 1 / Series 3: Key Object Selection with nested references --
    kos = Dataset()
    kos.file_meta = _file_meta(KeyObjectSelectionDocumentStorage, KOS1_UID, ExplicitVRLittleEndian)
    kos.SpecificCharacterSet = "ISO_IR 192"
    kos.SOPClassUID = KeyObjectSelectionDocumentStorage
    kos.SOPInstanceUID = KOS1_UID
    kos.StudyInstanceUID = STUDY1_UID
    kos.SeriesInstanceUID = SERIES3_UID
    kos.Modality = "KO"
    kos.InstanceNumber = 1
    kos.SeriesNumber = 3
    kos.PatientName = "PHANTOM^HEAD^A"
    kos.PatientID = "SYNTH-001"
    kos.AccessionNumber = "ACC00000016"
    kos.StudyID = "STUDY00016"
    kos.StudyDate = "20240115"
    kos.StudyTime = "120500"
    kos.StudyDescription = "合成体模研究一"
    kos.SeriesDescription = "关键对象 引用演示"
    kos.ContentDate = "20240115"
    kos.ContentTime = "120600"
    kos.ValueType = "CONTAINER"
    kos.ConceptNameCodeSequence = [Dataset()]
    kos.ConceptNameCodeSequence[0].CodeValue = "113000"
    kos.ConceptNameCodeSequence[0].CodingSchemeDesignator = "DCM"
    kos.ConceptNameCodeSequence[0].CodeMeaning = "Of Interest"
    # Nested three-level reference: study > series > SOP instances.
    ref_sops = []
    for ref_uid in (CT1_UID, CT2_UID, MISSING_REF_UID):
        sop_item = Dataset()
        sop_item.ReferencedSOPClassUID = CTImageStorage
        sop_item.ReferencedSOPInstanceUID = ref_uid
        ref_sops.append(sop_item)
    ref_series = Dataset()
    ref_series.SeriesInstanceUID = SERIES1_UID
    ref_series.ReferencedSOPSequence = ref_sops
    ref_study = Dataset()
    ref_study.StudyInstanceUID = STUDY1_UID
    ref_study.ReferencedSeriesSequence = [ref_series]
    kos.ReferencedStudySequence = [ref_study]
    archive.append(DemoInstance(kos, notes=["nested-references", "missing-reference"]))

    # --- Study 1 / Series 6: identifier conflict demo ---------------------
    # Same SOP Instance UID as ct1, different identity and pixels.
    ct1_conflict = _base_image_ds(
        patient_name="PHANTOM^HEAD^CONFLICT", patient_id="SYNTH-001-ALT",
        study_uid=STUDY1_UID, series_uid=SERIES6_UID, sop_uid=CT1_UID,
        sop_class=CTImageStorage, tsyntax=ExplicitVRLittleEndian,
        modality="CT", seed=61, instance_number=1, series_number=6,
        study_description="合成体模研究一", series_description="标识冲突演示组",
    )
    archive.append(DemoInstance(ct1_conflict, notes=["identifier-conflict"]))

    # --- Study 2 / Series 4: MR, Deflated Explicit VR LE ------------------
    for n, (uid, seed) in enumerate(((MR1_UID, 41), (MR2_UID, 42)), start=1):
        ds = _base_image_ds(
            patient_name="PHANTOM^CHEST^B", patient_id="SYNTH-002",
            study_uid=STUDY2_UID, series_uid=SERIES4_UID, sop_uid=uid,
            sop_class=MRImageStorage, tsyntax=DeflatedExplicitVRLittleEndian,
            modality="MR", seed=seed, instance_number=n, series_number=4,
            study_description="合成体模研究二", series_description="MR 压缩语法组",
        )
        _add_private_tags(ds)
        archive.append(DemoInstance(ds, notes=["deflated"]))

    # --- Study 2 / Series 5: file-meta / dataset identifier mismatch ------
    bad = _base_image_ds(
        patient_name="PHANTOM^CHEST^B", patient_id="SYNTH-002",
        study_uid=STUDY2_UID, series_uid=SERIES5_UID, sop_uid=BADMETA_UID,
        sop_class=CTImageStorage, tsyntax=ExplicitVRLittleEndian,
        modality="CT", seed=51, instance_number=1, series_number=5,
        study_description="合成体模研究二", series_description="元信息不一致演示组",
    )
    archive.append(
        DemoInstance(bad, command_set_uid=BADMETA_WRONG_UID, notes=["meta-mismatch"])
    )

    return archive
