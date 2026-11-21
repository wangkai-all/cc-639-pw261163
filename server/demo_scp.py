"""Demo imaging service: a tiny Query/Retrieve SCP with a synthetic archive.

Run standalone:  ``python -m server.demo_scp``

Supports C-ECHO, C-FIND (study/series/instance) and C-MOVE against the
programmatically generated archive from :mod:`server.demo_data`.

pynetdicom's C-MOVE SCP implementation manages the sub-operation
association itself and sends each yielded dataset with
``Association.send_c_store``. To simulate a non-conformant sender whose
C-STORE *command set* SOP Instance UID disagrees with the dataset (the
file-meta/dataset mismatch teaching sample), the demo service patches in
an Association subclass that mislabels exactly that one instance.
"""

from __future__ import annotations

import logging
import time
from io import BytesIO

from pydicom.dataset import Dataset
from pynetdicom import AE, evt
from pynetdicom.association import Association
from pynetdicom.dimse_primitives import C_STORE
from pynetdicom.dsutils import encode
from pynetdicom.sop_class import (
    Verification,
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage,
    KeyObjectSelectionDocumentStorage,
    BasicTextSRStorage,
    StudyRootQueryRetrieveInformationModelFind,
    StudyRootQueryRetrieveInformationModelMove,
)
from pydicom.uid import (
    ExplicitVRLittleEndian,
    ImplicitVRLittleEndian,
    DeflatedExplicitVRLittleEndian,
)

import pynetdicom.ae as _ae_module

from . import config
from .demo_data import BADMETA_UID, BADMETA_WRONG_UID, DemoInstance, build_demo_archive

LOGGER = logging.getLogger("demo_scp")

ALL_TRANSFER_SYNTAXES = [
    ImplicitVRLittleEndian,
    ExplicitVRLittleEndian,
    DeflatedExplicitVRLittleEndian,
]

STORAGE_CLASSES = [
    CTImageStorage,
    MRImageStorage,
    SecondaryCaptureImageStorage,
    KeyObjectSelectionDocumentStorage,
    BasicTextSRStorage,
]

#: dataset SOP Instance UID -> wrong UID to use in the C-STORE command set
COMMAND_SET_UID_OVERRIDES = {str(BADMETA_UID): str(BADMETA_WRONG_UID)}


class DemoAssociation(Association):
    """Association that mislabels the C-STORE command set for the one
    meta/dataset mismatch teaching sample (and behaves normally otherwise)."""

    def send_c_store(self, dataset, msg_id=1, priority=2, originator_aet=None,
                     originator_id=None):
        override = COMMAND_SET_UID_OVERRIDES.get(
            str(getattr(dataset, "SOPInstanceUID", ""))
        )
        if override is None:
            return super().send_c_store(
                dataset, msg_id=msg_id, priority=priority,
                originator_aet=originator_aet, originator_id=originator_id,
            )
        LOGGER.warning(
            "Sending %s with mismatched command-set UID %s (demo)",
            dataset.SOPInstanceUID, override,
        )
        return self._send_store_with_command_uid(
            dataset, override, msg_id, originator_aet, originator_id
        )

    def _send_store_with_command_uid(self, ds, affected_uid, msg_id,
                                     originator_aet, originator_id):
        """send_c_store equivalent with an overridden Affected SOP Instance
        UID in the C-STORE request command set."""
        context = self._get_valid_context(
            ds.SOPClassUID, ds.file_meta.TransferSyntaxUID, "scu"
        )
        tsyntax = context.transfer_syntax[0]
        req = C_STORE()
        req.MessageID = msg_id
        req.Priority = 2
        req.AffectedSOPClassUID = ds.SOPClassUID
        req.AffectedSOPInstanceUID = affected_uid
        req.MoveOriginatorApplicationEntityTitle = originator_aet
        req.MoveOriginatorMessageID = originator_id
        encoded = encode(
            ds, tsyntax.is_implicit_VR, tsyntax.is_little_endian, tsyntax.is_deflated
        )
        if encoded is None:
            raise ValueError("Failed to encode the supplied dataset")
        req.DataSet = BytesIO(encoded)

        self._reactor_checkpoint.clear()
        while not self._is_paused:
            time.sleep(0.0001)
        self.dimse.send_msg(req, context.context_id)
        _, rsp = self.dimse.get_msg(block=True)
        self._reactor_checkpoint.set()
        return self._check_received_status(rsp) if rsp is not None else Dataset()


_ae_module.Association = DemoAssociation


class DemoQRSCP:
    def __init__(self, archive: list[DemoInstance] | None = None):
        self.archive = archive if archive is not None else build_demo_archive()

    # ------------------------------------------------------------ matching
    def _match(self, identifier: Dataset) -> list[DemoInstance]:
        level = identifier.get("QueryRetrieveLevel", "")
        study = str(identifier.get("StudyInstanceUID", "") or "")
        series = str(identifier.get("SeriesInstanceUID", "") or "")
        instance = str(identifier.get("SOPInstanceUID", "") or "")

        def ok(inst: DemoInstance) -> bool:
            ds = inst.dataset
            if study and str(ds.StudyInstanceUID) != study:
                return False
            if level in ("SERIES", "IMAGE") and series and str(ds.SeriesInstanceUID) != series:
                return False
            if level == "IMAGE" and instance and str(ds.SOPInstanceUID) != instance:
                return False
            return True

        return [i for i in self.archive if ok(i)]

    # ------------------------------------------------------------- C-FIND
    def handle_find(self, event: evt.Event):
        identifier = event.identifier
        level = identifier.get("QueryRetrieveLevel", "")
        matches = self._match(identifier)

        if level == "STUDY":
            seen: dict[str, Dataset] = {}
            for inst in matches:
                ds = inst.dataset
                key = str(ds.StudyInstanceUID)
                if key not in seen:
                    rsp = Dataset()
                    rsp.QueryRetrieveLevel = "STUDY"
                    rsp.StudyInstanceUID = ds.StudyInstanceUID
                    rsp.PatientName = ds.get("PatientName", "")
                    rsp.PatientID = ds.get("PatientID", "")
                    rsp.StudyDescription = ds.get("StudyDescription", "")
                    rsp.StudyDate = ds.get("StudyDate", "")
                    rsp.AccessionNumber = ds.get("AccessionNumber", "")
                    rsp.ModalitiesInStudy = ds.get("Modality", "")
                    rsp.NumberOfStudyRelatedSeries = 0
                    rsp.NumberOfStudyRelatedInstances = 0
                    seen[key] = rsp
                row = seen[key]
                mods = set(row.ModalitiesInStudy) if row.ModalitiesInStudy else set()
                mods.add(str(ds.Modality))
                row.ModalitiesInStudy = sorted(mods)
                row.NumberOfStudyRelatedInstances += 1
                row.NumberOfStudyRelatedSeries = len(
                    {str(i.dataset.SeriesInstanceUID) for i in matches
                     if str(i.dataset.StudyInstanceUID) == key}
                )
            responses = list(seen.values())
        elif level == "SERIES":
            seen = {}
            for inst in matches:
                ds = inst.dataset
                key = str(ds.SeriesInstanceUID)
                if key not in seen:
                    rsp = Dataset()
                    rsp.QueryRetrieveLevel = "SERIES"
                    rsp.StudyInstanceUID = ds.StudyInstanceUID
                    rsp.SeriesInstanceUID = ds.SeriesInstanceUID
                    rsp.SeriesDescription = ds.get("SeriesDescription", "")
                    rsp.SeriesNumber = ds.get("SeriesNumber", "")
                    rsp.Modality = ds.get("Modality", "")
                    rsp.NumberOfSeriesRelatedInstances = 0
                    seen[key] = rsp
                seen[key].NumberOfSeriesRelatedInstances += 1
            responses = list(seen.values())
        else:  # IMAGE
            responses = []
            for inst in matches:
                ds = inst.dataset
                rsp = Dataset()
                rsp.QueryRetrieveLevel = "IMAGE"
                rsp.StudyInstanceUID = ds.StudyInstanceUID
                rsp.SeriesInstanceUID = ds.SeriesInstanceUID
                rsp.SOPInstanceUID = ds.SOPInstanceUID
                rsp.SOPClassUID = ds.SOPClassUID
                rsp.InstanceNumber = ds.get("InstanceNumber", "")
                # Demo-service convenience: tell the workstation which
                # transfer syntax this instance will arrive in.
                rsp.TransferSyntaxUID = ds.file_meta.TransferSyntaxUID
                if inst.command_set_uid:
                    rsp.InstanceAvailabilityNote = "META-MISMATCH-DEMO"
                responses.append(rsp)

        for rsp in responses:
            rsp.SpecificCharacterSet = "ISO_IR 192"
            yield 0xFF00, rsp
        yield 0x0000, None

    # ------------------------------------------------------------- C-MOVE
    def handle_move(self, event: evt.Event):
        """Framework-managed C-MOVE: yield the destination, the number of
        matches, then each dataset to store (pynetdicom performs the
        C-STORE sub-operations, via DemoAssociation for the override)."""
        request = event.request
        destination = str(request.MoveDestination).strip()
        if destination not in config.MOVE_DESTINATIONS:
            LOGGER.error("Unknown move destination %s", destination)
            yield (None, None)
            return
        host, port = config.MOVE_DESTINATIONS[destination]
        yield (host, port)

        matches = self._match(event.identifier)
        yield len(matches)

        for inst in matches:
            yield 0xFF00, inst.dataset
        yield 0x0000, None


def make_server(archive: list[DemoInstance] | None = None):
    scp = DemoQRSCP(archive)
    ae = AE(ae_title=config.DEMO_AE_TITLE)
    ae.add_supported_context(Verification)
    ae.add_supported_context(
        StudyRootQueryRetrieveInformationModelFind, ExplicitVRLittleEndian
    )
    ae.add_supported_context(
        StudyRootQueryRetrieveInformationModelMove, ExplicitVRLittleEndian
    )
    # The C-MOVE sub-operation association is opened by pynetdicom via
    # AE.associate, which needs requested storage contexts to propose.
    # One context per (class, transfer syntax) so each instance keeps its
    # native transfer syntax instead of being converted to the first
    # mutually supported one.
    for cx in STORAGE_CLASSES:
        for ts in ALL_TRANSFER_SYNTAXES:
            ae.add_requested_context(cx, [ts])
    handlers = [
        (evt.EVT_C_FIND, scp.handle_find),
        (evt.EVT_C_MOVE, scp.handle_move),
    ]
    return ae, handlers


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s %(message)s")
    ae, handlers = make_server()
    LOGGER.info(
        "Demo imaging service %s listening on %s:%s (synthetic archive: %d instances)",
        config.DEMO_AE_TITLE, config.DEMO_HOST, config.DEMO_PORT,
        len(build_demo_archive()),
    )
    ae.start_server(
        (config.DEMO_HOST, config.DEMO_PORT), block=True, evt_handlers=handlers
    )


if __name__ == "__main__":
    main()
