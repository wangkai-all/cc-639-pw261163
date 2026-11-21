"""FastAPI application tying the workstation together.

Endpoints cover the full flow: query the demo imaging service, retrieve
series into the local store, inspect instances (structured fields,
references, pixel preview), review conflicts/quarantine/reports, and
export an anonymized download package.
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from pydicom.dataset import Dataset

from . import config
from .anonymize import AnonymizeStats, UIDMapper, anonymize_dataset, rules_document
from .pixel import has_pixels, render_png
from .receiver import Receiver
from .references import build_integrity_report, extract_references
from .store import InstanceStore, encode_file_bytes

app = FastAPI(title="DICOM 收件工作站（合成样本演示）")
store = InstanceStore()
receiver = Receiver(store)


@app.on_event("startup")
def _startup() -> None:
    receiver.start()


@app.on_event("shutdown")
def _shutdown() -> None:
    receiver.shutdown()


# --------------------------------------------------------------------------
# Remote query (demo imaging service)
# --------------------------------------------------------------------------


@app.get("/api/health")
def health() -> dict[str, Any]:
    return {"ok": True, "scp": receiver._server is not None}


@app.get("/api/remote/studies")
def remote_studies() -> list[dict[str, Any]]:
    try:
        return receiver.find_studies()
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/remote/studies/{study_uid}/series")
def remote_series(study_uid: str) -> list[dict[str, Any]]:
    try:
        return receiver.find_series(study_uid)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


@app.get("/api/remote/series/instances")
def remote_instances(study_uid: str, series_uid: str) -> list[dict[str, Any]]:
    try:
        return receiver.find_instances(study_uid, series_uid)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


class RetrieveRequest(BaseModel):
    study_uid: str
    series_uid: str


@app.post("/api/retrieve")
def retrieve(req: RetrieveRequest) -> dict[str, Any]:
    try:
        return receiver.cmove_series(req.study_uid, req.series_uid)
    except RuntimeError as exc:
        raise HTTPException(status_code=502, detail=str(exc))


# --------------------------------------------------------------------------
# Local store
# --------------------------------------------------------------------------


@app.get("/api/studies")
def local_hierarchy() -> list[dict[str, Any]]:
    return store.hierarchy()


@app.get("/api/instances/{sop_uid}")
def instance_detail(sop_uid: str) -> dict[str, Any]:
    entry = store.get_entry(sop_uid)
    if not entry:
        raise HTTPException(404, "实例不存在")
    ds = store.load_dataset(sop_uid)
    detail: dict[str, Any] = {
        "entry": entry,
        "fields": _dataset_tree(ds) if ds is not None else None,
        "has_pixels": bool(ds is not None and has_pixels(ds)),
    }
    return detail


@app.get("/api/instances/{sop_uid}/references")
def instance_references(sop_uid: str) -> dict[str, Any]:
    entry = store.get_entry(sop_uid)
    if not entry:
        raise HTTPException(404, "实例不存在")
    ds = store.load_dataset(sop_uid)
    outgoing = extract_references(ds) if ds is not None else []
    instances = store.all_instances()
    present = {u for u, e in instances.items() if e.get("status") in ("stored", "conflict")}
    for r in outgoing:
        r["status"] = "present" if r["referenced_sop_instance_uid"] in present else "missing"
    incoming = []
    for uid, e in instances.items():
        if uid == sop_uid or e.get("status") == "quarantined":
            continue
        other = store.load_dataset(uid)
        if other is None:
            continue
        if any(
            ref["referenced_sop_instance_uid"] == sop_uid
            for ref in extract_references(other)
        ):
            incoming.append(uid)
    return {"outgoing": outgoing, "incoming": incoming}


@app.get("/api/instances/{sop_uid}/pixel.png")
def instance_pixel(sop_uid: str) -> Response:
    ds = store.load_dataset(sop_uid)
    if ds is None:
        raise HTTPException(404, "实例不存在")
    if not has_pixels(ds):
        raise HTTPException(404, "该实例不含像素数据")
    try:
        png = render_png(ds)
    except Exception as exc:  # noqa: BLE001 - surface as 415 to the UI
        raise HTTPException(415, f"无法渲染像素预览: {exc}")
    return Response(content=png, media_type="image/png")


@app.get("/api/conflicts")
def conflicts() -> list[dict[str, Any]]:
    return store.conflicts()


@app.get("/api/quarantine")
def quarantine() -> list[dict[str, Any]]:
    return store.quarantined()


@app.get("/api/reports/receiving")
def receiving_report() -> list[dict[str, Any]]:
    return store.receive_log()


@app.get("/api/reports/integrity")
def integrity_report() -> dict[str, Any]:
    return build_integrity_report(store)


@app.get("/api/anonymization/rules")
def anonymization_rules() -> dict[str, Any]:
    return rules_document()


@app.post("/api/reset")
def reset_store() -> dict[str, str]:
    store.reset()
    return {"status": "cleared"}


# --------------------------------------------------------------------------
# Export package
# --------------------------------------------------------------------------


@app.post("/api/export")
def export_package() -> FileResponse:
    path = build_export(store)
    return FileResponse(
        path,
        media_type="application/zip",
        filename=path.name,
    )


def build_export(st: InstanceStore) -> Path:
    """Build the download package: layered structured tables, anonymized
    files, reference integrity report and an identity-free processing
    summary."""
    mapper = UIDMapper()
    stats = AnonymizeStats()
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")

    instances = st.all_instances()
    exportable = {
        uid: e for uid, e in instances.items() if e.get("status") in ("stored", "conflict")
    }
    quarantined_count = sum(1 for e in instances.values() if e.get("status") == "quarantined")
    conflict_uids = [uid for uid, e in exportable.items() if e.get("status") == "conflict"]

    anon: dict[str, Dataset] = {}
    for uid in sorted(exportable):
        ds = st.load_dataset(uid)
        if ds is None:
            continue
        anonymize_dataset(ds, mapper, stats)
        anon[uid] = ds

    # --- integrity report over the anonymized set (mapped UIDs) -----------
    mapped_present = {str(ds.SOPInstanceUID) for ds in anon.values() if "SOPInstanceUID" in ds}
    integrity_rows = []
    missing_rows = []
    for uid, ds in sorted(anon.items()):
        refs = extract_references(ds)
        for r in refs:
            status = "present" if r["referenced_sop_instance_uid"] in mapped_present else "missing"
            row = {
                "from_instance": str(ds.SOPInstanceUID),
                "path": r["path"],
                "referenced_sop_instance_uid": r["referenced_sop_instance_uid"],
                "referenced_sop_class_uid": r["referenced_sop_class_uid"],
                "status": status,
            }
            integrity_rows.append(row)
            if status == "missing":
                missing_rows.append(row)

    # --- layered tables (anonymized identifiers only) ---------------------
    studies: dict[str, dict[str, Any]] = {}
    series: dict[str, dict[str, Any]] = {}
    inst_rows: list[dict[str, Any]] = []
    for uid, ds in sorted(anon.items()):
        su = str(getattr(ds, "StudyInstanceUID", ""))
        se = str(getattr(ds, "SeriesInstanceUID", ""))
        studies.setdefault(
            su,
            {
                "study_instance_uid": su,
                "study_description": str(getattr(ds, "StudyDescription", "") or ""),
                "modalities_in_study": set(),
                "series_count": set(),
                "instance_count": 0,
            },
        )
        st_row = studies[su]
        st_row["modalities_in_study"].add(str(getattr(ds, "Modality", "") or ""))
        st_row["series_count"].add(se)
        st_row["instance_count"] += 1
        series.setdefault(
            se,
            {
                "series_instance_uid": se,
                "study_instance_uid": su,
                "modality": str(getattr(ds, "Modality", "") or ""),
                "series_number": str(getattr(ds, "SeriesNumber", "") or ""),
                "series_description": str(getattr(ds, "SeriesDescription", "") or ""),
                "instance_count": 0,
            },
        )
        series[se]["instance_count"] += 1
        inst_rows.append(
            {
                "sop_instance_uid": str(getattr(ds, "SOPInstanceUID", "")),
                "sop_class_uid": str(getattr(ds, "SOPClassUID", "")),
                "series_instance_uid": se,
                "study_instance_uid": su,
                "instance_number": str(getattr(ds, "InstanceNumber", "") or ""),
                "transfer_syntax_uid": str(ds.file_meta.get("TransferSyntaxUID", "")),
                "rows": str(getattr(ds, "Rows", "") or ""),
                "columns": str(getattr(ds, "Columns", "") or ""),
                "had_conflict": "yes" if uid in conflict_uids else "no",
            }
        )

    study_rows = [
        {
            **{k: v for k, v in r.items() if k not in ("modalities_in_study", "series_count")},
            "modalities_in_study": "/".join(sorted(r["modalities_in_study"])),
            "series_count": len(r["series_count"]),
        }
        for r in studies.values()
    ]

    # --- write the zip -----------------------------------------------------
    out_path = config.EXPORT_DIR / f"export_{ts}.zip"
    with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
        for name, rows in (
            ("studies", study_rows),
            ("series", list(series.values())),
            ("instances", inst_rows),
        ):
            zf.writestr(f"tables/{name}.json", json.dumps(rows, ensure_ascii=False, indent=1))
            zf.writestr(f"tables/{name}.csv", _rows_to_csv(rows))

        for uid, ds in sorted(anon.items()):
            su = str(getattr(ds, "StudyInstanceUID", "unknown-study"))
            se = str(getattr(ds, "SeriesInstanceUID", "unknown-series"))
            iu = str(getattr(ds, "SOPInstanceUID", uid))
            zf.writestr(
                f"anonymized/{su}/{se}/{iu}.dcm",
                encode_file_bytes(ds),
            )

        zf.writestr(
            "reports/reference_integrity.json",
            json.dumps(
                {
                    "summary": {
                        "instances": len(anon),
                        "total_references": len(integrity_rows),
                        "missing_references": len(missing_rows),
                    },
                    "references": integrity_rows,
                    "missing": missing_rows,
                },
                ensure_ascii=False,
                indent=1,
            ),
        )
        zf.writestr(
            "reports/anonymization_rules.json",
            json.dumps(rules_document(), ensure_ascii=False, indent=1),
        )
        zf.writestr(
            "reports/processing_summary.json",
            json.dumps(
                {
                    "generated_at": ts,
                    "package": "anonymized export (no original identity included)",
                    "instances_exported": len(anon),
                    "instances_quarantined_excluded": quarantined_count,
                    "instances_with_conflicts_active_version_only": len(conflict_uids),
                    "anonymization": stats.as_dict(),
                    "mapped_uids": sorted(mapper.mapping.values()),
                    "note": (
                        "本摘要不含任何原始身份信息；原始 UID 到匿名 UID 的映射表"
                        "不随包导出。"
                    ),
                },
                ensure_ascii=False,
                indent=1,
            ),
        )
    return out_path


def _rows_to_csv(rows: list[dict[str, Any]]) -> str:
    if not rows:
        return ""
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    writer.writeheader()
    writer.writerows(rows)
    return buf.getvalue()


# --------------------------------------------------------------------------
# Structured field tree
# --------------------------------------------------------------------------

_IDENTITY_HINTS = (
    "Patient", "Physician", "Institution", "Station", "Accession", "Operator",
    "StudyID", "Device", "Date", "Time", "Name", "Address", "Comments",
)


def _dataset_tree(ds: Dataset, depth: int = 0, max_depth: int = 8) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if depth > max_depth:
        return rows
    if depth == 0 and getattr(ds, "file_meta", None):
        rows.extend(_dataset_tree(ds.file_meta, depth + 1, max_depth))
    for elem in ds:
        keyword = elem.keyword or ""
        row: dict[str, Any] = {
            "tag": str(elem.tag),
            "keyword": keyword,
            "vr": elem.VR,
            "private": elem.tag.is_private,
            "group": _group_for(elem, keyword),
        }
        if elem.VR == "SQ":
            row["value"] = f"<{len(elem.value)} 项>"
            row["children"] = [
                {"item": i, "rows": _dataset_tree(item, depth + 1, max_depth)}
                for i, item in enumerate(elem.value)
            ]
        elif keyword == "PixelData":
            row["value"] = f"<像素数据 {len(elem.value) if elem.value else 0} 字节>"
        else:
            value = str(elem.value) if elem.value is not None else ""
            row["value"] = value if len(value) <= 200 else value[:200] + "…"
        rows.append(row)
    return rows


def _group_for(elem, keyword: str) -> str:
    if elem.tag.group == 0x0002:
        return "文件元信息"
    if elem.tag.is_private:
        return "私有字段"
    if keyword in ("StudyInstanceUID", "SeriesInstanceUID", "SOPInstanceUID",
                   "FrameOfReferenceUID") or "Referenced" in keyword:
        return "标识与引用"
    if any(h in keyword for h in _IDENTITY_HINTS):
        return "身份与描述"
    return "采集参数与其他"


# --------------------------------------------------------------------------
# Frontend
# --------------------------------------------------------------------------

app.mount("/", StaticFiles(directory=config.BASE_DIR / "static", html=True), name="static")
