#!/usr/bin/env python3
"""Convert a format-v3 WSI file into standard cBioPortal resource files.

The converter is deliberately offline: it never connects to cBioPortal or an
artifact store. It preserves the importer-validated WSI row fields in JSON and
creates the two resource entity levels that Resource Data v2 supports.
"""

import argparse
import csv
import json
from pathlib import Path
from urllib.parse import quote


SAMPLE_RESOURCE_ID = "WSI_SAMPLE"
PATIENT_RESOURCE_ID = "WSI_PATIENT"
PUBLIC_FIELDS = {
    "IMAGE_ID", "PART_KEY", "PART_NUMBER", "PART_DESIGNATOR", "PART_TYPE",
    "PART_DESCRIPTION", "SUBSPECIALTY", "PATH_DX_TITLE", "BLOCK_KEY",
    "BLOCK_NUMBER", "BLOCK_LABEL", "MATCH_LEVEL", "SPECIMEN_KEY", "STAIN_NAME",
    "STAIN_GROUP", "IS_HNE", "IS_IHC", "MAGNIFICATION", "FILE_SIZE_BYTES",
    "BARCODE", "SLIDE_TYPE", "CAN_SERVE_TILES", "TIMELINE_START_DAYS",
    "TIMELINE_DATE_STATUS", "TIMELINE_DATE_KIND", "TIMELINE_DATE_SOURCE",
    "TIMELINE_DATE_REASON", "TIMELINE_COORDINATE_SYSTEM", "TIMEPOINT_SOURCE",
}
SERVING_FIELDS = {
    "SOURCE_URL", "TILE_METADATA_JSON", "THUMBNAIL_URL", "THUMBNAIL_WIDTH",
    "THUMBNAIL_HEIGHT", "THUMBNAIL_CONTENT_TYPE",
}
BOOLEAN_FIELDS = {"IS_HNE", "IS_IHC", "CAN_SERVE_TILES"}
INTEGER_FIELDS = {"FILE_SIZE_BYTES", "TIMELINE_START_DAYS", "THUMBNAIL_WIDTH", "THUMBNAIL_HEIGHT"}


def meta_value(path: Path, key: str) -> str:
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith(key + ":"):
            return line.split(":", 1)[1].strip()
    raise ValueError(f"{path}: missing {key}")


def text(value: str | None) -> str | None:
    return value if value not in (None, "", "NA") else None


def metadata(row: dict[str, str]) -> str:
    def typed(key: str) -> object | None:
        raw = text(row.get(key))
        if raw is None:
            return None
        if key in BOOLEAN_FIELDS:
            if raw.upper() not in {"TRUE", "FALSE"}:
                raise ValueError(f"{key} must be TRUE or FALSE")
            return raw.upper() == "TRUE"
        if key in INTEGER_FIELDS:
            return int(raw)
        return raw

    value = {key.lower(): typed(key) for key in PUBLIC_FIELDS if typed(key) is not None}
    serving = {key.lower(): typed(key) for key in SERVING_FIELDS if typed(key) is not None}
    if "tile_metadata_json" in serving:
        serving["tile_metadata_json"] = json.loads(serving["tile_metadata_json"])
    value["wsi_serving"] = serving
    return json.dumps(value, separators=(",", ":"), sort_keys=True)


def write_meta(path: Path, study_id: str, resource_type: str, data_file: str) -> None:
    path.write_text(
        f"cancer_study_identifier: {study_id}\nresource_type: {resource_type}\ndata_filename: {data_file}\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--meta-wsi", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    study_id = meta_value(args.meta_wsi, "cancer_study_identifier")
    data_path = args.meta_wsi.parent / meta_value(args.meta_wsi, "data_filename")
    args.output_dir.mkdir(parents=True, exist_ok=True)
    with data_path.open(encoding="utf-8", newline="") as source:
        for _ in range(4):
            next(source)
        rows = list(csv.DictReader(source, delimiter="\t"))
    sample_rows, patient_rows = [], []
    for row in rows:
        patient_id, image_id = text(row.get("PATIENT_ID")), text(row.get("IMAGE_ID"))
        if not patient_id or not image_id:
            raise ValueError("WSI rows require PATIENT_ID and IMAGE_ID")
        target = sample_rows if text(row.get("SAMPLE_ID")) else patient_rows
        entity_id = text(row.get("SAMPLE_ID")) or patient_id
        resource_id = SAMPLE_RESOURCE_ID if target is sample_rows else PATIENT_RESOURCE_ID
        url = f"/patient/{quote(patient_id, safe='')}/wsi?studyId={quote(study_id, safe='')}&imageId={quote(image_id, safe='')}"
        target.append((entity_id, resource_id, url, image_id, "WHOLE_SLIDE_IMAGE", metadata(row)))
    definition = args.output_dir / "data_resource_definition.txt"
    definition.write_text(
        "RESOURCE_ID\tDISPLAY_NAME\tDESCRIPTION\tRESOURCE_TYPE\tOPEN_BY_DEFAULT\tPRIORITY\tCUSTOM_METADATA\n"
        f"{SAMPLE_RESOURCE_ID}\tPathology slides\tWhole-slide images linked to samples\tSAMPLE\tFALSE\t1\t{{}}\n"
        f"{PATIENT_RESOURCE_ID}\tPathology slides\tWhole-slide images linked to patients\tPATIENT\tFALSE\t1\t{{}}\n",
        encoding="utf-8",
    )
    write_meta(args.output_dir / "meta_resource_definition.txt", study_id, "DEFINITION", definition.name)
    for entity, file_name, resource_type, values in (
        ("SAMPLE_ID", "data_resource_sample.txt", "SAMPLE", sample_rows),
        ("PATIENT_ID", "data_resource_patient.txt", "PATIENT", patient_rows),
    ):
        with (args.output_dir / file_name).open("w", encoding="utf-8", newline="") as target:
            writer = csv.writer(target, delimiter="\t", lineterminator="\n")
            writer.writerow([entity, "RESOURCE_ID", "URL", "DISPLAY_NAME", "TYPE", "METADATA"])
            writer.writerows(values)
        write_meta(args.output_dir / f"meta_resource_{resource_type.lower()}.txt", study_id, resource_type, file_name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
