"""Local instance store.

Tracks received instances at the study / series / instance levels, detects
identifier conflicts (same SOP Instance UID, different content -> never
silently overwritten) and quarantines instances whose reconstructed file
meta identifiers disagree with the dataset identifiers.

The index is a JSON document; instance files live under ``data/received``
and ``data/quarantine``.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import threading
from datetime import datetime, timezone
from io import BytesIO
from pathlib import Path
from typing import Any

from pydicom.dataset import Dataset

from . import config


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _safe_uid(uid: str) -> str:
    return "".join(c if c.isalnum() or c in ".-" else "_" for c in uid)


def encode_file_bytes(ds: Dataset) -> bytes:
    """Deterministically encode a dataset (with file meta) to bytes."""
    buf = BytesIO()
    ds.save_as(buf, enforce_file_format=True)
    return buf.getvalue()


class InstanceStore:
    def __init__(self, index_path: Path | None = None):
        self.index_path = index_path or config.INDEX_PATH
        self._lock = threading.RLock()
        self._index: dict[str, Any] = {"instances": {}, "receive_log": []}
        self._load()

    # ------------------------------------------------------------- persistence
    def _load(self) -> None:
        with self._lock:
            if self.index_path.exists():
                self._index = json.loads(self.index_path.read_text(encoding="utf-8"))
            else:
                self._index = {"instances": {}, "receive_log": []}

    def _save(self) -> None:
        with self._lock:
            tmp = self.index_path.with_suffix(".tmp")
            tmp.write_text(
                json.dumps(self._index, ensure_ascii=False, indent=1), encoding="utf-8"
            )
            tmp.replace(self.index_path)

    # ----------------------------------------------------------------- receive
    def begin_receive(self, study_uid: str, series_uid: str) -> str:
        with self._lock:
            op_id = f"op-{len(self._index['receive_log']) + 1:04d}"
            self._index["receive_log"].append(
                {
                    "id": op_id,
                    "study_uid": study_uid,
                    "series_uid": series_uid,
                    "started_at": _now(),
                    "finished_at": None,
                    "results": [],
                }
            )
            self._save()
            return op_id

    def finish_receive(self, op_id: str) -> None:
        with self._lock:
            op = self._op(op_id)
            if op:
                op["finished_at"] = _now()
                counts: dict[str, int] = {}
                for r in op["results"]:
                    counts[r["status"]] = counts.get(r["status"], 0) + 1
                op["summary"] = counts
                self._save()

    def _op(self, op_id: str) -> dict[str, Any] | None:
        for op in self._index["receive_log"]:
            if op["id"] == op_id:
                return op
        return None

    def receive_instance(
        self,
        ds: Dataset,
        *,
        source_ae: str,
        op_id: str | None = None,
    ) -> dict[str, Any]:
        """Store one received dataset. Never overwrites differing content.

        ``ds.file_meta`` must already be reconstructed from the C-STORE
        command set and the negotiated presentation context by the caller.
        """
        with self._lock:
            ds_uid = str(getattr(ds, "SOPInstanceUID", "") or "")
            ds_class = str(getattr(ds, "SOPClassUID", "") or "")
            meta_uid = str(ds.file_meta.get("MediaStorageSOPInstanceUID", ""))
            meta_class = str(ds.file_meta.get("MediaStorageSOPClassUID", ""))
            tsyntax = str(ds.file_meta.get("TransferSyntaxUID", ""))

            payload = encode_file_bytes(ds)
            digest = hashlib.sha256(payload).hexdigest()

            result: dict[str, Any] = {
                "sop_instance_uid": ds_uid,
                "sop_class_uid": ds_class,
                "study_uid": str(getattr(ds, "StudyInstanceUID", "") or ""),
                "series_uid": str(getattr(ds, "SeriesInstanceUID", "") or ""),
                "received_at": _now(),
                "source_ae": source_ae,
                "content_hash": digest,
            }

            # --- Quarantine: file meta identifiers vs dataset identifiers ---
            mismatch = {}
            if meta_uid and ds_uid and meta_uid != ds_uid:
                mismatch["MediaStorageSOPInstanceUID"] = {"file_meta": meta_uid, "dataset": ds_uid}
            if meta_class and ds_class and meta_class != ds_class:
                mismatch["MediaStorageSOPClassUID"] = {"file_meta": meta_class, "dataset": ds_class}
            if mismatch:
                qpath = config.QUARANTINE_DIR / f"{_safe_uid(ds_uid)}.{digest[:8]}.dcm"
                qpath.write_bytes(payload)
                entry = self._index["instances"].get(ds_uid)
                record = {
                    "status": "quarantined",
                    "meta_mismatch": mismatch,
                    "file": str(qpath.relative_to(config.DATA_DIR)),
                    "transfer_syntax": tsyntax,
                    "versions": [
                        {
                            "hash": digest,
                            "file": str(qpath.relative_to(config.DATA_DIR)),
                            "received_at": result["received_at"],
                            "source_ae": source_ae,
                        }
                    ],
                    **self._describe(ds),
                }
                if entry:
                    entry.setdefault("quarantine_records", []).append(record)
                else:
                    self._index["instances"][ds_uid] = record
                self._save()
                result.update(status="quarantined", detail=mismatch)
                self._log_result(op_id, result)
                return result

            entry = self._index["instances"].get(ds_uid)
            if entry is None:
                fpath = config.RECEIVED_DIR / f"{_safe_uid(ds_uid)}.dcm"
                fpath.write_bytes(payload)
                self._index["instances"][ds_uid] = {
                    "status": "stored",
                    "file": str(fpath.relative_to(config.DATA_DIR)),
                    "transfer_syntax": tsyntax,
                    "content_hash": digest,
                    "versions": [
                        {
                            "hash": digest,
                            "file": str(fpath.relative_to(config.DATA_DIR)),
                            "received_at": result["received_at"],
                            "source_ae": source_ae,
                            "active": True,
                        }
                    ],
                    **self._describe(ds),
                }
                self._save()
                result.update(status="stored")
                self._log_result(op_id, result)
                return result

            if entry.get("status") == "quarantined":
                # A clean copy of a previously quarantined UID arrived.
                fpath = config.RECEIVED_DIR / f"{_safe_uid(ds_uid)}.dcm"
                fpath.write_bytes(payload)
                quarantine_records = entry.pop("quarantine_records", [])
                entry.update(
                    {
                        "status": "stored",
                        "file": str(fpath.relative_to(config.DATA_DIR)),
                        "transfer_syntax": tsyntax,
                        "content_hash": digest,
                        "versions": [
                            {
                                "hash": digest,
                                "file": str(fpath.relative_to(config.DATA_DIR)),
                                "received_at": result["received_at"],
                                "source_ae": source_ae,
                                "active": True,
                            }
                        ],
                        "prior_quarantine": quarantine_records,
                        **self._describe(ds),
                    }
                )
                entry.pop("meta_mismatch", None)
                self._save()
                result.update(status="stored", detail="previously quarantined UID now stored cleanly")
                self._log_result(op_id, result)
                return result

            # Same UID already stored: compare content.
            if entry.get("content_hash") == digest:
                result.update(status="duplicate", detail="identical content already stored")
                self._log_result(op_id, result)
                return result

            # Conflict: same identifier, different content. Keep both.
            existing_hashes = {v["hash"] for v in entry.get("versions", [])}
            if digest in existing_hashes:
                result.update(status="duplicate", detail="content matches an earlier conflict version")
                self._log_result(op_id, result)
                return result

            version_no = len(entry["versions"]) + 1
            fpath = config.RECEIVED_DIR / f"{_safe_uid(ds_uid)}.v{version_no}.dcm"
            fpath.write_bytes(payload)
            entry["versions"].append(
                {
                    "hash": digest,
                    "file": str(fpath.relative_to(config.DATA_DIR)),
                    "received_at": result["received_at"],
                    "source_ae": source_ae,
                    "active": False,
                }
            )
            entry["status"] = "conflict"
            entry["conflict"] = self._diff_versions(entry)
            self._save()
            result.update(status="conflict", detail=entry["conflict"])
            self._log_result(op_id, result)
            return result

    def _log_result(self, op_id: str | None, result: dict[str, Any]) -> None:
        if not op_id:
            return
        op = self._op(op_id)
        if op is not None:
            op["results"].append(result)
            self._save()

    @staticmethod
    def _describe(ds: Dataset) -> dict[str, Any]:
        return {
            "sop_instance_uid": str(getattr(ds, "SOPInstanceUID", "") or ""),
            "sop_class_uid": str(getattr(ds, "SOPClassUID", "") or ""),
            "study_uid": str(getattr(ds, "StudyInstanceUID", "") or ""),
            "series_uid": str(getattr(ds, "SeriesInstanceUID", "") or ""),
            "modality": str(getattr(ds, "Modality", "") or ""),
            "patient_name": str(getattr(ds, "PatientName", "") or ""),
            "patient_id": str(getattr(ds, "PatientID", "") or ""),
            "study_description": str(getattr(ds, "StudyDescription", "") or ""),
            "series_description": str(getattr(ds, "SeriesDescription", "") or ""),
            "series_number": str(getattr(ds, "SeriesNumber", "") or ""),
            "instance_number": str(getattr(ds, "InstanceNumber", "") or ""),
        }

    def _diff_versions(self, entry: dict[str, Any]) -> dict[str, Any]:
        """Field-level diff between the active version and the newest one."""
        import pydicom

        fields = [
            "PatientName", "PatientID", "Modality", "SeriesInstanceUID",
            "StudyInstanceUID", "InstanceNumber", "SeriesNumber",
            "ContentDate", "StudyDescription", "SeriesDescription",
        ]
        versions = entry.get("versions", [])
        if len(versions) < 2:
            return {}
        base = pydicom.dcmread(config.DATA_DIR / versions[0]["file"])
        new = pydicom.dcmread(config.DATA_DIR / versions[-1]["file"])
        diffs = {}
        for kw in fields:
            v0 = str(getattr(base, kw, "") or "")
            v1 = str(getattr(new, kw, "") or "")
            if v0 != v1:
                diffs[kw] = {"stored": v0, "received": v1}
        import hashlib as _h

        p0 = getattr(base, "PixelData", None)
        p1 = getattr(new, "PixelData", None)
        if p0 is not None and p1 is not None:
            h0 = _h.sha256(bytes(p0)).hexdigest()[:16]
            h1 = _h.sha256(bytes(p1)).hexdigest()[:16]
            if h0 != h1:
                diffs["PixelData"] = {"stored": f"sha256:{h0}…", "received": f"sha256:{h1}…"}
        return diffs

    # ------------------------------------------------------------------ query
    def hierarchy(self) -> list[dict[str, Any]]:
        """Study -> series -> instance tree of stored/conflict instances."""
        with self._lock:
            studies: dict[str, Any] = {}
            for uid, inst in self._index["instances"].items():
                if inst.get("status") == "quarantined":
                    continue
                st = studies.setdefault(
                    inst["study_uid"],
                    {
                        "study_uid": inst["study_uid"],
                        "study_description": inst.get("study_description", ""),
                        "patient_name": inst.get("patient_name", ""),
                        "patient_id": inst.get("patient_id", ""),
                        "series": {},
                    },
                )
                se = st["series"].setdefault(
                    inst["series_uid"],
                    {
                        "series_uid": inst["series_uid"],
                        "series_description": inst.get("series_description", ""),
                        "series_number": inst.get("series_number", ""),
                        "modality": inst.get("modality", ""),
                        "instances": [],
                    },
                )
                se["instances"].append(
                    {
                        "sop_instance_uid": uid,
                        "sop_class_uid": inst.get("sop_class_uid", ""),
                        "instance_number": inst.get("instance_number", ""),
                        "status": inst.get("status"),
                        "transfer_syntax": inst.get("transfer_syntax", ""),
                        "content_hash": inst.get("content_hash", "")[:12],
                    }
                )
            out = []
            for st in studies.values():
                st["series"] = sorted(
                    st["series"].values(), key=lambda s: (s["series_number"], s["series_uid"])
                )
                for se in st["series"]:
                    se["instances"].sort(
                        key=lambda i: (i["instance_number"], i["sop_instance_uid"])
                    )
                out.append(st)
            out.sort(key=lambda s: s["study_uid"])
            return out

    def get_entry(self, sop_uid: str) -> dict[str, Any] | None:
        with self._lock:
            return self._index["instances"].get(sop_uid)

    def load_dataset(self, sop_uid: str, version_file: str | None = None) -> Dataset | None:
        import pydicom

        entry = self.get_entry(sop_uid)
        if not entry:
            return None
        rel = version_file or entry.get("file")
        if not rel:
            return None
        path = config.DATA_DIR / rel
        if not path.exists():
            return None
        return pydicom.dcmread(path)

    def conflicts(self) -> list[dict[str, Any]]:
        with self._lock:
            return [
                {"sop_instance_uid": uid, **{k: e.get(k) for k in (
                    "study_uid", "series_uid", "modality", "patient_name",
                    "versions", "conflict", "series_description")}}
                for uid, e in self._index["instances"].items()
                if e.get("status") == "conflict"
            ]

    def quarantined(self) -> list[dict[str, Any]]:
        with self._lock:
            out = []
            for uid, e in self._index["instances"].items():
                if e.get("status") == "quarantined":
                    out.append(
                        {
                            "sop_instance_uid": uid,
                            "study_uid": e.get("study_uid"),
                            "series_uid": e.get("series_uid"),
                            "meta_mismatch": e.get("meta_mismatch"),
                            "versions": e.get("versions"),
                        }
                    )
                for rec in e.get("quarantine_records", []) or []:
                    out.append(
                        {
                            "sop_instance_uid": uid,
                            "study_uid": rec.get("study_uid"),
                            "series_uid": rec.get("series_uid"),
                            "meta_mismatch": rec.get("meta_mismatch"),
                            "versions": rec.get("versions"),
                            "superseded": True,
                        }
                    )
            return out

    def receive_log(self) -> list[dict[str, Any]]:
        with self._lock:
            return list(reversed(self._index["receive_log"]))

    def all_instances(self) -> dict[str, dict[str, Any]]:
        with self._lock:
            return dict(self._index["instances"])

    def reset(self) -> None:
        with self._lock:
            self._index = {"instances": {}, "receive_log": []}
            for d in (config.RECEIVED_DIR, config.QUARANTINE_DIR, config.EXPORT_DIR):
                if d.exists():
                    shutil.rmtree(d)
                d.mkdir(parents=True, exist_ok=True)
            self._save()
