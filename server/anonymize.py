"""Anonymization engine with an explicit, inspectable rule set.

Rule groups (exposed via :func:`rules_document` and embedded in every
export package):

1. **remove** — identity / demographic / free-text fields and all
   date/time fields. Dropped unconditionally.
2. **keep** — acquisition and geometry parameters that are retained as-is
   (documented; everything not otherwise ruled is also kept).
3. **uid_remap** — *identity* UIDs only: study / series / instance /
   frame-of-reference UIDs and every reference to them, wherever they
   appear (top level, nested sequences, cross-instance references, and the
   file-meta Media Storage SOP Instance UID, which is synced to the
   remapped dataset SOP Instance UID). One mapping per export run, so
   references stay consistent across files.
4. **type identifiers are never remapped** — SOP Class UIDs (including
   Referenced SOP Class UID), Transfer Syntax UID, Coding Scheme UID and
   any other well-known UID stay untouched. Transfer Syntax UID in
   particular always describes the actual encoding; nothing here
   transcodes pixel data, so the original value is preserved.
5. **private tags** — removed by default (configurable allowlist, empty by
   default). This applies to *all* private tags, not just fields that
   happen to be visible in the UI.
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Any

from pydicom.dataset import Dataset
from pydicom.uid import UID
from pydicom.uid import UID_dictionary

# ---------------------------------------------------------------------------
# Rule tables
# ---------------------------------------------------------------------------

#: 1. Fields removed unconditionally (identity, demographics, free text,
#:    dates/times that could re-identify).
REMOVE_KEYWORDS: list[str] = [
    # Patient identity / demographics
    "PatientName", "PatientID", "IssuerOfPatientID", "OtherPatientIDs",
    "OtherPatientNames", "PatientBirthName", "PatientMotherBirthName",
    "PatientBirthDate", "PatientBirthTime", "PatientSex", "PatientAge",
    "PatientSize", "PatientWeight", "PatientAddress", "PatientComments",
    "PatientTelephoneNumbers", "PatientReligiousPreference",
    "MedicalRecordLocator", "ReferencedPatientSequence",
    # Care-team / institution identity
    "ReferringPhysicianName", "ReferringPhysicianIdentificationSequence",
    "PerformingPhysicianName", "PerformingPhysicianIdentificationSequence",
    "PhysiciansOfRecord", "PhysiciansOfRecordIdentificationSequence",
    "OperatorsName", "OperatorIdentificationSequence",
    "InstitutionName", "InstitutionAddress", "InstitutionalDepartmentName",
    "InstitutionCodeSequence", "StationName",
    "RequestingPhysician", "RequestingPhysicianIdentificationSequence",
    "RequestingService", "RequestingServiceCodeSequence",
    "NameOfPhysiciansReadingStudy", "ConsultingPhysicianName",
    "PersonIdentificationCodeSequence",
    # Order / scheduling identifiers
    "AccessionNumber", "StudyID", "AdmissionID", "ServiceEpisodeID",
    "IssuerOfAdmissionID", "IssuerOfServiceEpisodeID",
    "RequestedProcedureID", "ScheduledProcedureStepID",
    "PlacerOrderNumberImagingServiceRequest",
    "FillerOrderNumberImagingServiceRequest",
    # Device identity
    "DeviceSerialNumber", "DeviceUID", "PlateID", "DetectorID",
    "GantryID", "CassetteID",
    # Dates / times (all of them)
    "StudyDate", "StudyTime", "SeriesDate", "SeriesTime",
    "AcquisitionDate", "AcquisitionTime", "ContentDate", "ContentTime",
    "InstanceCreationDate", "InstanceCreationTime",
    "AcquisitionDateTime", "InstanceCoercionDateTime",
    "PerformedProcedureStepStartDate", "PerformedProcedureStepStartTime",
    "PerformedProcedureStepEndDate", "PerformedProcedureStepEndTime",
    "DateOfLastCalibration", "TimeOfLastCalibration",
    "FrameAcquisitionDateTime", "FrameReferenceDateTime",
    "VerificationDateTime", "ObservationDateTime",
    "PresentationCreationDate", "PresentationCreationTime",
    # Free text that may carry identity
    "ImageComments", "StudyComments", "AdditionalPatientHistory",
    "PatientState", "AdmittingDiagnosesDescription",
    "DischargeDiagnosisDescription", "VisitComments",
    "DerivationDescription", "SeriesDescriptionCodeSequence",
    "ReasonForTheRequestedProcedure", "ReasonForTheImagingProcedure",
    "RequestedProcedureDescription", "RequestedProcedureComments",
    "ImagingServiceRequestComments", "ConfidentialityConstraintOnPatientDataDescription",
    "HumanPerformerName", "HumanPerformerOrganization",
    "ScheduledPerformingPhysicianName", "ScheduledProcedureStepDescription",
]

#: 2. Acquisition / geometry parameters explicitly retained (documentation
#:    list — the anonymizer keeps everything not ruled out, these are the
#:    parameters the UI highlights as "retained acquisition parameters").
KEEP_ACQUISITION_KEYWORDS: list[str] = [
    "Modality", "ImageType", "InstanceNumber", "SeriesNumber",
    "Rows", "Columns", "BitsAllocated", "BitsStored", "HighBit",
    "PixelRepresentation", "SamplesPerPixel", "PhotometricInterpretation",
    "PlanarConfiguration", "PixelSpacing", "ImagerPixelSpacing",
    "SliceThickness", "SpacingBetweenSlices", "SliceLocation",
    "ImagePositionPatient", "ImageOrientationPatient",
    "WindowCenter", "WindowWidth", "WindowCenterWidthExplanation",
    "RescaleIntercept", "RescaleSlope", "RescaleType",
    "KVP", "XRayTubeCurrent", "XRayTubeCurrentInmA", "ExposureTime",
    "Exposure", "ExposureInmAs", "FilterType", "FocalSpots",
    "ConvolutionKernel", "ReconstructionDiameter", "ScanOptions",
    "RepetitionTime", "EchoTime", "InversionTime", "EchoNumbers",
    "MagneticFieldStrength", "FlipAngle", "SequenceName",
    "ScanningSequence", "SequenceVariant", "ImagedNucleus",
    "NumberOfAverages", "PercentPhaseFieldOfView", "PixelBandwidth",
    "BodyPartExamined", "ViewPosition", "PatientPosition",
    "Laterality", "ImageLaterality", "SliceProgressionDirection",
    "NumberOfFrames", "FrameIncrementPointer",
    "BurnedInAnnotation", "LossyImageCompression",
    "SpecificCharacterSet", "ConversionType",
]

#: 3. Identity UIDs that are remapped (by keyword, matched recursively).
REMAP_UID_KEYWORDS: list[str] = [
    "StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
    "FrameOfReferenceUID", "SynchronizationFrameOfReferenceUID",
    "ReferencedFrameOfReferenceUID", "ReferencedSOPInstanceUID",
    "ReferencedFrameOfReferenceSequence",  # handled via children
    "MediaStorageSOPInstanceUID",          # file meta, synced
    "StorageMediaFileSetUID", "ReferencedStorageMediaSequence",
    "FailedSOPInstanceUIDList", "ReferencedSOPInstanceUIDInFile",
    "TemplateExtensionCreatorUID", "SourceImageSequence",  # via children
]

#: UID keywords that are *type identifiers* and must never be remapped.
NEVER_REMAP_UID_KEYWORDS: list[str] = [
    "SOPClassUID", "ReferencedSOPClassUID", "MediaStorageSOPClassUID",
    "TransferSyntaxUID", "AffectedSOPClassUID", "RequestedSOPClassUID",
    "CodingSchemeUID", "ContextUID", "MappingResourceUID",
    "ImplementationClassUID", "ReferencedPresentationStateSequence",
]

#: 5. Private tags: default policy.
PRIVATE_TAG_DEFAULT = "remove"   # "remove" | "keep"
PRIVATE_TAG_ALLOWLIST: list[str] = []  # e.g. ["0011,10xx"] — empty by default

#: Well-known UIDs (SOP classes, transfer syntaxes, coding schemes, …) are
#: type identifiers, never identity — never remapped regardless of keyword.
_WELL_KNOWN_UIDS = set(UID_dictionary.keys())


def rules_document() -> dict[str, Any]:
    """The anonymization rule set, as shipped in exports and shown in the UI."""
    return {
        "version": "1.0",
        "description": (
            "Synthetic-demo anonymization rules. Identity/demographic/date fields "
            "are removed; acquisition parameters are retained; identity UIDs "
            "(study/series/instance/frame-of-reference and all references to them, "
            "including nested sequences and the file-meta Media Storage SOP "
            "Instance UID) are remapped with one consistent mapping per export; "
            "type identifiers (SOP Class UID, Transfer Syntax UID, well-known "
            "UIDs) are preserved; Transfer Syntax UID always matches the actual "
            "encoding because no transcoding is performed; private tags are "
            "removed by default."
        ),
        "remove_keywords": REMOVE_KEYWORDS,
        "keep_acquisition_keywords": KEEP_ACQUISITION_KEYWORDS,
        "uid_remap": {
            "remapped_keywords": REMAP_UID_KEYWORDS,
            "never_remapped_keywords": NEVER_REMAP_UID_KEYWORDS,
            "well_known_uids_preserved": True,
            "mapping_scope": "per export run; identical original UID -> identical "
                             "mapped UID across all files, nested sequences and "
                             "cross-instance references; file-meta Media Storage "
                             "SOP Instance UID synced to the dataset mapping",
            "mapped_uid_root": "2.25.<uuid5(salt, original)>",
        },
        "private_tags": {
            "default": PRIVATE_TAG_DEFAULT,
            "allowlist": PRIVATE_TAG_ALLOWLIST,
        },
        "transfer_syntax_policy": (
            "Never transcoded. TransferSyntaxUID in file meta is copied "
            "verbatim from the received instance."
        ),
    }


# ---------------------------------------------------------------------------
# UID mapping
# ---------------------------------------------------------------------------


class UIDMapper:
    """Deterministic-per-run identity UID mapper (2.25 arc)."""

    def __init__(self, salt: str | None = None):
        self.salt = salt or uuid.uuid4().hex
        self._map: dict[str, str] = {}

    def map(self, uid: str) -> str:
        uid = str(uid).strip()
        if not uid:
            return uid
        if uid not in self._map:
            mapped_int = uuid.uuid5(uuid.NAMESPACE_URL, f"{self.salt}|{uid}").int
            self._map[uid] = f"2.25.{mapped_int}"
        return self._map[uid]

    @property
    def mapping(self) -> dict[str, str]:
        return dict(self._map)


# ---------------------------------------------------------------------------
# Anonymizer
# ---------------------------------------------------------------------------


@dataclass
class AnonymizeStats:
    removed_fields: dict[str, int] = field(default_factory=dict)
    removed_private_tags: int = 0
    remapped_uids: dict[str, int] = field(default_factory=dict)  # keyword -> count
    kept_type_uids: dict[str, int] = field(default_factory=dict)
    sequences_walked: int = 0

    def _bump(self, table: dict[str, int], key: str) -> None:
        table[key] = table.get(key, 0) + 1

    def merge(self, other: "AnonymizeStats") -> None:
        for k, v in other.removed_fields.items():
            self.removed_fields[k] = self.removed_fields.get(k, 0) + v
        for k, v in other.remapped_uids.items():
            self.remapped_uids[k] = self.remapped_uids.get(k, 0) + v
        for k, v in other.kept_type_uids.items():
            self.kept_type_uids[k] = self.kept_type_uids.get(k, 0) + v
        self.removed_private_tags += other.removed_private_tags
        self.sequences_walked += other.sequences_walked

    def as_dict(self) -> dict[str, Any]:
        return {
            "removed_fields": dict(sorted(self.removed_fields.items())),
            "removed_private_tags": self.removed_private_tags,
            "remapped_uid_fields": dict(sorted(self.remapped_uids.items())),
            "preserved_type_uid_fields": dict(sorted(self.kept_type_uids.items())),
            "sequences_walked": self.sequences_walked,
        }


def _is_well_known(uid: str) -> bool:
    return str(uid) in _WELL_KNOWN_UIDS


def anonymize_dataset(
    ds: Dataset,
    mapper: UIDMapper,
    stats: AnonymizeStats | None = None,
) -> Dataset:
    """Anonymize one dataset in place (it is already a copy of the stored
    file). Returns the dataset. ``stats`` accumulates across calls."""
    if stats is None:
        stats = AnonymizeStats()
    _walk(ds, mapper, stats)
    _fix_file_meta(ds, mapper, stats)
    return ds


def _walk(ds: Dataset, mapper: UIDMapper, stats: AnonymizeStats) -> None:
    for elem in list(ds):
        tag = elem.tag
        keyword = elem.keyword

        # 5. private tags — default rule applies to ALL private tags.
        if tag.is_private:
            del ds[tag]
            stats.removed_private_tags += 1
            continue

        # 1. removal list
        if keyword and keyword in REMOVE_KEYWORDS:
            del ds[tag]
            stats._bump(stats.removed_fields, keyword)
            continue

        # 3/4. UID handling
        if elem.VR == "UI":
            value = str(elem.value or "").strip()
            if not value:
                continue
            if keyword in NEVER_REMAP_UID_KEYWORDS or _is_well_known(value):
                stats._bump(stats.kept_type_uids, keyword or str(tag))
                continue
            if keyword in REMAP_UID_KEYWORDS or not _is_well_known(value):
                # identity UID (explicit list, or unknown UID -> fail-safe remap)
                elem.value = UID(mapper.map(value))
                stats._bump(stats.remapped_uids, keyword or str(tag))
                continue

        # recurse into sequences (nested references)
        if elem.VR == "SQ":
            stats.sequences_walked += 1
            for item in elem.value:
                _walk(item, mapper, stats)


def _fix_file_meta(ds: Dataset, mapper: UIDMapper, stats: AnonymizeStats) -> None:
    """Sync file meta: Media Storage SOP Instance UID follows the dataset
    mapping; class/transfer-syntax identifiers are preserved verbatim."""
    if not hasattr(ds, "file_meta") or ds.file_meta is None:
        return
    meta = ds.file_meta
    if "MediaStorageSOPInstanceUID" in meta:
        original = str(meta.MediaStorageSOPInstanceUID)
        # Map through the same mapper so file meta matches the dataset's
        # (already remapped) SOP Instance UID.
        if "SOPInstanceUID" in ds:
            meta.MediaStorageSOPInstanceUID = UID(ds.SOPInstanceUID)
        else:
            meta.MediaStorageSOPInstanceUID = UID(mapper.map(original))
        stats._bump(stats.remapped_uids, "MediaStorageSOPInstanceUID")
    # MediaStorageSOPClassUID / TransferSyntaxUID: preserved (no transcoding).
    for kw in ("MediaStorageSOPClassUID", "TransferSyntaxUID"):
        if kw in meta:
            stats._bump(stats.kept_type_uids, kw)
    # Sender identity in file meta is dropped.
    for kw in ("SourceApplicationEntityTitle", "SendingApplicationEntityTitle",
               "ReceivingApplicationEntityTitle"):
        if kw in meta:
            del meta[kw]
            stats._bump(stats.removed_fields, kw)
