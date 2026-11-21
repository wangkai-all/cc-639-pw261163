"""DICOM networking for the workstation.

* An embedded C-STORE SCP (runs in a background thread next to FastAPI)
  receives instances pushed by the demo service's C-MOVE sub-operations.
  File meta is reconstructed from the C-STORE command set and the
  negotiated presentation context — exactly the place where a command-set /
  dataset identifier mismatch shows up, so the store can quarantine it.
* SCU helpers perform C-FIND (study/series/instance) and C-MOVE against
  the demo imaging service.
"""

from __future__ import annotations

import threading
import time
from typing import Any

from pydicom.dataset import Dataset, FileMetaDataset
from pynetdicom import AE, evt
from pynetdicom.ae import ThreadedAssociationServer
from pynetdicom.sop_class import (
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

from . import config
from .store import InstanceStore

ACCEPTED_TRANSFER_SYNTAXES = [
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


class Receiver:
    """Owns the embedded C-STORE SCP and the SCU client helpers."""

    def __init__(self, store: InstanceStore):
        self.store = store
        self._server: ThreadedAssociationServer | None = None
        self._lock = threading.Lock()
        self.current_op_id: str | None = None

    # ---------------------------------------------------------- C-STORE SCP
    def start(self) -> None:
        ae = AE(ae_title=config.WS_AE_TITLE)
        for cx in STORAGE_CLASSES:
            ae.add_supported_context(cx, ACCEPTED_TRANSFER_SYNTAXES)
        handlers = [(evt.EVT_C_STORE, self._on_c_store)]
        self._server = ae.start_server(
            (config.WS_CSTORE_HOST, config.WS_CSTORE_PORT),
            block=False,
            evt_handlers=handlers,
        )

    def shutdown(self) -> None:
        if self._server:
            self._server.shutdown()
            self._server = None

    def _on_c_store(self, event: evt.Event) -> int:
        ds = event.dataset
        tsyntax = event.context.transfer_syntax

        # Reconstruct file meta from the command set + negotiated context.
        meta = FileMetaDataset()
        meta.MediaStorageSOPClassUID = event.request.AffectedSOPClassUID
        meta.MediaStorageSOPInstanceUID = event.request.AffectedSOPInstanceUID
        meta.TransferSyntaxUID = tsyntax
        ds.file_meta = meta

        with self._lock:
            op_id = self.current_op_id
        self.store.receive_instance(
            ds,
            source_ae=str(event.assoc.requestor.ae_title).strip(),
            op_id=op_id,
        )
        return 0x0000

    # ------------------------------------------------------------- SCU side
    def _associate(self, *contexts) -> "Any":
        ae = AE(ae_title=config.WS_AE_TITLE)
        for cx in contexts:
            ae.add_requested_context(cx, ExplicitVRLittleEndian)
        assoc = ae.associate(
            config.DEMO_HOST, config.DEMO_PORT, ae_title=config.DEMO_AE_TITLE
        )
        if not assoc.is_established:
            raise RuntimeError(
                f"无法连接演示影像服务 {config.DEMO_AE_TITLE} "
                f"@ {config.DEMO_HOST}:{config.DEMO_PORT}（请先启动 demo_scp）"
            )
        return assoc

    def cfind(self, level: str, identifier: Dataset) -> list[Dataset]:
        identifier.QueryRetrieveLevel = level
        assoc = self._associate(StudyRootQueryRetrieveInformationModelFind)
        out: list[Dataset] = []
        try:
            for status, rsp in assoc.send_c_find(
                identifier, StudyRootQueryRetrieveInformationModelFind
            ):
                if status and status.Status in (0xFF00, 0xFF01) and rsp is not None:
                    out.append(rsp)
        finally:
            assoc.release()
        return out

    def find_studies(self) -> list[dict[str, Any]]:
        q = Dataset()
        for kw in ("PatientName", "PatientID", "StudyInstanceUID", "StudyDescription",
                   "StudyDate", "ModalitiesInStudy", "NumberOfStudyRelatedSeries",
                   "NumberOfStudyRelatedInstances", "AccessionNumber"):
            setattr(q, kw, "")
        rows = self.cfind("STUDY", q)
        return [_ds_to_dict(r) for r in rows]

    def find_series(self, study_uid: str) -> list[dict[str, Any]]:
        q = Dataset()
        q.StudyInstanceUID = study_uid
        for kw in ("SeriesInstanceUID", "SeriesDescription", "SeriesNumber",
                   "Modality", "NumberOfSeriesRelatedInstances"):
            setattr(q, kw, "")
        rows = self.cfind("SERIES", q)
        return [_ds_to_dict(r) for r in rows]

    def find_instances(self, study_uid: str, series_uid: str) -> list[dict[str, Any]]:
        q = Dataset()
        q.StudyInstanceUID = study_uid
        q.SeriesInstanceUID = series_uid
        for kw in ("SOPInstanceUID", "SOPClassUID", "InstanceNumber"):
            setattr(q, kw, "")
        rows = self.cfind("IMAGE", q)
        return [_ds_to_dict(r) for r in rows]

    def cmove_series(self, study_uid: str, series_uid: str) -> dict[str, Any]:
        """C-MOVE one series to this workstation's store SCP."""
        identifier = Dataset()
        identifier.QueryRetrieveLevel = "SERIES"
        identifier.StudyInstanceUID = study_uid
        identifier.SeriesInstanceUID = series_uid

        op_id = self.store.begin_receive(study_uid, series_uid)
        with self._lock:
            self.current_op_id = op_id

        assoc = self._associate(StudyRootQueryRetrieveInformationModelMove)
        final_status: int | None = None
        counters: dict[str, int] = {}
        try:
            for status, _ in assoc.send_c_move(
                identifier, config.WS_AE_TITLE,
                StudyRootQueryRetrieveInformationModelMove,
            ):
                if status is None:
                    continue
                final_status = status.Status
                for kw, key in (
                    ("NumberOfRemainingSuboperations", "remaining"),
                    ("NumberOfCompletedSuboperations", "completed"),
                    ("NumberOfFailedSuboperations", "failed"),
                    ("NumberOfWarningSuboperations", "warning"),
                ):
                    if kw in status:
                        counters[key] = int(status.get(kw))
        finally:
            assoc.release()
            # Give the SCP thread a moment to finish logging the last store.
            time.sleep(0.05)
            with self._lock:
                self.current_op_id = None
            self.store.finish_receive(op_id)

        return {
            "op_id": op_id,
            "final_status": f"0x{final_status:04X}" if final_status is not None else None,
            "suboperations": counters,
        }


def _ds_to_dict(ds: Dataset) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for elem in ds:
        if not elem.keyword:
            continue
        value = elem.value
        if value is None:
            out[elem.keyword] = ""
        elif isinstance(value, (list, tuple)) or type(value).__name__ == "MultiValue":
            out[elem.keyword] = [str(v) for v in value]
        else:
            out[elem.keyword] = str(value)
    return out
