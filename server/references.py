"""Reference extraction and integrity reporting.

Walks received instances recursively and collects every SOP-instance
reference (wherever ReferencedSOPInstanceUID appears, including deeply
nested sequences) plus frame-of-reference usage. The integrity report
cross-checks references against what is actually stored locally.
"""

from __future__ import annotations

from typing import Any

from pydicom.dataset import Dataset

from .store import InstanceStore


def extract_references(ds: Dataset) -> list[dict[str, Any]]:
    """All SOP-instance references in a dataset, with their nesting path."""
    refs: list[dict[str, Any]] = []
    _walk(ds, "dataset", refs)
    return refs


def _walk(ds: Dataset, path: str, refs: list[dict[str, Any]]) -> None:
    for elem in ds:
        if elem.VR == "SQ":
            for i, item in enumerate(elem.value):
                _walk(item, f"{path} > {elem.keyword or elem.tag}[{i}]", refs)
        elif elem.keyword == "ReferencedSOPInstanceUID" and elem.value:
            # Sibling ReferencedSOPClassUID gives the referenced type.
            sop_class = str(getattr(ds, "ReferencedSOPClassUID", "") or "")
            refs.append(
                {
                    "path": path,
                    "referenced_sop_instance_uid": str(elem.value),
                    "referenced_sop_class_uid": sop_class,
                }
            )


def frame_of_reference_uids(ds: Dataset) -> list[str]:
    out = []
    for kw in ("FrameOfReferenceUID", "SynchronizationFrameOfReferenceUID"):
        v = getattr(ds, kw, None)
        if v:
            out.append(str(v))
    return out


def build_integrity_report(store: InstanceStore) -> dict[str, Any]:
    """Reference integrity over everything currently stored.

    For every stored/conflict instance: its outgoing references and whether
    each target is present locally. Missing targets are listed explicitly.
    Quarantined instances are excluded from the graph but listed.
    """
    instances = store.all_instances()
    present_uids = {
        uid for uid, e in instances.items() if e.get("status") in ("stored", "conflict")
    }

    per_instance: list[dict[str, Any]] = []
    missing: list[dict[str, Any]] = []
    incoming: dict[str, list[str]] = {}

    for uid, entry in sorted(instances.items()):
        if entry.get("status") == "quarantined":
            continue
        ds = store.load_dataset(uid)
        if ds is None:
            continue
        refs = extract_references(ds)
        ref_rows = []
        for r in refs:
            target = r["referenced_sop_instance_uid"]
            status = "present" if target in present_uids else "missing"
            ref_rows.append({**r, "status": status})
            incoming.setdefault(target, []).append(uid)
            if status == "missing":
                missing.append(
                    {
                        "from_instance": uid,
                        "from_series": entry.get("series_uid"),
                        "missing_sop_instance_uid": target,
                        "referenced_sop_class_uid": r["referenced_sop_class_uid"],
                        "path": r["path"],
                    }
                )
        per_instance.append(
            {
                "sop_instance_uid": uid,
                "sop_class_uid": entry.get("sop_class_uid"),
                "study_uid": entry.get("study_uid"),
                "series_uid": entry.get("series_uid"),
                "status": entry.get("status"),
                "references": ref_rows,
            }
        )

    incoming_rows = [
        {"sop_instance_uid": uid, "referenced_by": sorted(srcs)}
        for uid, srcs in sorted(incoming.items())
    ]

    return {
        "summary": {
            "stored_instances": len(present_uids),
            "quarantined_instances": sum(
                1 for e in instances.values() if e.get("status") == "quarantined"
            ),
            "conflict_instances": sum(
                1 for e in instances.values() if e.get("status") == "conflict"
            ),
            "total_references": sum(len(p["references"]) for p in per_instance),
            "missing_references": len(missing),
        },
        "instances": per_instance,
        "missing": missing,
        "incoming": incoming_rows,
    }
