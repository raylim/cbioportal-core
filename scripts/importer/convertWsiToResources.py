#!/usr/bin/env python3
"""Convert a legacy format-v3 WSI file pair into standard cBioPortal study files.

The converter is deliberately offline: it never connects to cBioPortal, a
database, or an artifact store. It reads ``meta_wsi.txt``/``data_wsi.txt``,
applies the same row parsing and normalization as the retired native importer
(``ImportWsiData``), and writes:

* ``data_resource_definition.txt`` with the ``WSI_SAMPLE``/``WSI_PATIENT``
  definitions that have rows;
* ``data_resource_sample.txt`` for slides matched to a sample (``PART`` or
  ``BLOCK``) and ``data_resource_patient.txt`` for unmatched slides; each row
  links to the standalone viewer and carries the slide metadata as JSON;
* ``data_clinical_sample_wsi_counts.txt`` and
  ``data_clinical_patient_wsi_counts.txt`` with the six ``WSI_*`` slide-count
  attributes the native importer used to generate;

each with its meta file. A data/meta pair is only written when it has rows.
Timeline files are not produced: existing clinical timeline files stay in the
study and are imported unchanged.

The output is not validated here beyond what the native importer checked while
parsing; run ``validateData.py`` on the study after adding the files.
"""

import argparse
import json
import sys
from pathlib import Path
from urllib.parse import quote, urlparse


SAMPLE_RESOURCE_ID = "WSI_SAMPLE"
PATIENT_RESOURCE_ID = "WSI_PATIENT"
RESOURCE_TYPE = "WHOLE_SLIDE_IMAGE"

# Must stay identical to ImportWsiData.COLUMNS (format version 3).
COLUMNS = [
    "PATIENT_ID", "REFERENCE_SAMPLE_ID", "SAMPLE_ID", "IMAGE_ID",
    "PART_KEY", "PART_NUMBER", "PART_DESIGNATOR", "PART_TYPE",
    "PART_DESCRIPTION", "SUBSPECIALTY", "PATH_DX_TITLE", "BLOCK_KEY",
    "BLOCK_NUMBER", "BLOCK_LABEL", "MATCH_LEVEL", "SPECIMEN_KEY",
    "STAIN_NAME", "STAIN_GROUP", "IS_HNE", "IS_IHC", "MAGNIFICATION",
    "FILE_SIZE_BYTES", "BARCODE", "SLIDE_TYPE", "CAN_SERVE_TILES", "SOURCE_URL",
    "TILE_METADATA_JSON", "THUMBNAIL_URL", "THUMBNAIL_WIDTH",
    "THUMBNAIL_HEIGHT", "THUMBNAIL_CONTENT_TYPE", "TIMELINE_START_DAYS",
    "TIMELINE_DATE_STATUS", "TIMELINE_DATE_KIND", "TIMELINE_DATE_SOURCE",
    "TIMELINE_DATE_REASON", "TIMELINE_COORDINATE_SYSTEM", "TIMEPOINT_SOURCE",
]

# Public metadata keys, emitted in lower case. Values the backend reads with
# JSONExtractString stay strings (e.g. PART_NUMBER, MAGNIFICATION).
PUBLIC_STRING_FIELDS = [
    "IMAGE_ID", "PART_KEY", "PART_NUMBER", "PART_DESIGNATOR", "PART_TYPE",
    "PART_DESCRIPTION", "SUBSPECIALTY", "PATH_DX_TITLE", "BLOCK_KEY",
    "BLOCK_NUMBER", "BLOCK_LABEL", "MATCH_LEVEL", "SPECIMEN_KEY", "STAIN_NAME",
    "STAIN_GROUP", "MAGNIFICATION", "BARCODE", "SLIDE_TYPE",
    "TIMELINE_DATE_STATUS", "TIMELINE_DATE_KIND", "TIMELINE_DATE_SOURCE",
    "TIMELINE_DATE_REASON", "TIMELINE_COORDINATE_SYSTEM",
]
SERVING_KEY = "wsi_serving"

MATCH_LEVELS = ("BLOCK", "PART", "UNMATCHED")
TIMELINE_STATUSES = ("AVAILABLE", "MISSING_PROCEDURE_DATE", "MISSING_REFERENCE_SEQUENCING_DATE")
TIMELINE_KINDS = ("RECORDED", "ESTIMATED", "UNDATED")
TIMELINE_COORDINATE_SYSTEM = "patient_first_tumor_sequencing_day_zero"

# Names and descriptions must stay identical to ImportWsiData.insertSampleSlideCounts.
SAMPLE_COUNT_ATTRIBUTES = [
    ("WSI_SAMPLE_SLIDE_COUNT", "WSI Slides per Sample",
     "Associated pathology slide count for the sample."),
    ("WSI_SAMPLE_PART_MATCHED_SLIDE_COUNT", "WSI Slides per Sample, Part-matched",
     "Associated pathology slides matched to a specimen part."),
    ("WSI_SAMPLE_BLOCK_MATCHED_SLIDE_COUNT", "WSI Slides per Sample, Block-matched",
     "Associated pathology slides matched to a specimen block."),
]
PATIENT_COUNT_ATTRIBUTES = [
    ("WSI_PATIENT_SLIDE_COUNT", "WSI Slides per Patient",
     "Associated pathology slide count for the patient."),
    ("WSI_PATIENT_PART_MATCHED_SLIDE_COUNT", "WSI Slides per Patient, Part-matched",
     "Associated pathology slides matched to a specimen part for the patient."),
    ("WSI_PATIENT_BLOCK_MATCHED_SLIDE_COUNT", "WSI Slides per Patient, Block-matched",
     "Associated pathology slides matched to a specimen block for the patient."),
]
COUNT_ATTRIBUTE_IDS = frozenset(
    attribute[0] for attribute in SAMPLE_COUNT_ATTRIBUTES + PATIENT_COUNT_ATTRIBUTES)

DEFINITION_FILE = "data_resource_definition.txt"
SAMPLE_RESOURCE_FILE = "data_resource_sample.txt"
PATIENT_RESOURCE_FILE = "data_resource_patient.txt"
SAMPLE_COUNTS_FILE = "data_clinical_sample_wsi_counts.txt"
PATIENT_COUNTS_FILE = "data_clinical_patient_wsi_counts.txt"
RESOURCE_HEADER_SAMPLE = ["PATIENT_ID", "SAMPLE_ID", "RESOURCE_ID", "URL", "DISPLAY_NAME", "TYPE", "METADATA"]
RESOURCE_HEADER_PATIENT = ["PATIENT_ID", "RESOURCE_ID", "URL", "DISPLAY_NAME", "TYPE", "METADATA"]


class ConversionError(ValueError):
    """Raised when the legacy input cannot be converted faithfully."""


def read_meta(path):
    """Parse a cBioPortal ``key: value`` meta file."""
    values = {}
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as error:
        raise ConversionError(f"{path}: cannot read meta file: {error}") from error
    for line in lines:
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if ":" not in line:
            raise ConversionError(f"{path}: invalid meta line: {line!r}")
        key, value = line.split(":", 1)
        values[key.strip()] = value.strip()
    return values


def read_wsi_meta(path):
    meta = read_meta(path)
    if meta.get("genetic_alteration_type") != "PATHOLOGY_SLIDES" or meta.get("datatype") != "WSI":
        raise ConversionError(f"{path}: WSI metadata must use PATHOLOGY_SLIDES / WSI")
    if meta.get("format_version") != "3":
        raise ConversionError(f"{path}: unsupported WSI format_version; expected 3")
    for field in ("cancer_study_identifier", "data_filename"):
        if not meta.get(field):
            raise ConversionError(f"{path}: {field} is required")
    return meta


def normalize_base_url(value):
    """Return the portal base URL without a trailing slash, or raise."""
    try:
        parsed = urlparse(value)
    except ValueError as error:
        raise ConversionError(f"--portal-base-url is not a valid URL: {value!r}") from error
    if parsed.scheme not in ("http", "https") or not parsed.netloc:
        raise ConversionError(
            f"--portal-base-url must be an absolute http(s) URL, for example "
            f"https://portal.example.org or https://example.org/cbioportal: {value!r}")
    if parsed.query or parsed.fragment or parsed.params:
        raise ConversionError(f"--portal-base-url must not contain a query or fragment: {value!r}")
    return value.rstrip("/")


def viewer_url(base_url, study_id, patient_id, image_id):
    """Absolute link to the standalone viewer route for one slide."""
    return (f"{base_url}/wsi/patient/{quote(patient_id, safe='')}"
            f"?studyId={quote(study_id, safe='')}&imageId={quote(image_id, safe='')}")


def read_rows(data_path):
    """Read the legacy data file: leading '#' rows, the exact v3 header, then slide rows."""
    try:
        with open(data_path, encoding="utf-8", newline="") as stream:
            lines = stream.read().split("\n")
    except OSError as error:
        raise ConversionError(f"{data_path}: cannot read WSI data file: {error}") from error
    except UnicodeDecodeError as error:
        raise ConversionError(f"{data_path}: file is not valid UTF-8") from error
    if lines and lines[-1] == "":
        lines.pop()
    lines = [line[:-1] if line.endswith("\r") else line for line in lines]
    index = 0
    while index < len(lines) and lines[index].startswith("#"):
        index += 1
    if index >= len(lines) or lines[index].split("\t") != COLUMNS:
        raise ConversionError(f"{data_path}: WSI data has an invalid header or column order")
    rows = []
    for line_number, line in enumerate(lines[index + 1:], start=index + 2):
        if line.startswith("#"):
            raise ConversionError(f"{data_path}: line {line_number}: WSI data row must not start with '#'")
        fields = line.split("\t")
        if len(fields) != len(COLUMNS):
            raise ConversionError(
                f"{data_path}: line {line_number}: expected {len(COLUMNS)} columns, found {len(fields)}")
        if all(not field.strip() for field in fields):
            raise ConversionError(f"{data_path}: line {line_number}: blank WSI row")
        rows.append((line_number, dict(zip(COLUMNS, (field.strip() for field in fields)))))
    if not rows:
        raise ConversionError(f"{data_path}: WSI data file contains no slide rows")
    return rows


def _fail(line, message):
    raise ConversionError(f"line {line}: {message}")


def _require(row, field, line):
    if not row[field]:
        _fail(line, f"{field} is required")
    return row[field]


def _boolean(row, field, line):
    if row[field] not in ("TRUE", "FALSE"):
        _fail(line, f"{field} must be TRUE or FALSE")
    return row[field] == "TRUE"


def _optional_int(row, field, line):
    if not row[field]:
        return None
    try:
        return int(row[field])
    except ValueError:
        _fail(line, f"invalid {field}")


def _validate_timing(start_days, status, kind, source, reason, coordinate_system, line):
    # Mirrors ImportWsiData.validateTiming.
    if status not in TIMELINE_STATUSES:
        _fail(line, "invalid TIMELINE_DATE_STATUS")
    if kind not in TIMELINE_KINDS:
        _fail(line, "invalid TIMELINE_DATE_KIND")
    if not source:
        _fail(line, "TIMELINE_DATE_SOURCE is required")
    if coordinate_system != TIMELINE_COORDINATE_SYSTEM:
        _fail(line, "unsupported TIMELINE_COORDINATE_SYSTEM")
    if status == "AVAILABLE":
        if start_days is None or kind == "UNDATED" or reason:
            _fail(line, "AVAILABLE timing is inconsistent")
        return
    if start_days is not None:
        _fail(line, "non-AVAILABLE timing cannot have TIMELINE_START_DAYS")
    if status == "MISSING_PROCEDURE_DATE" and kind != "UNDATED":
        _fail(line, "missing procedure date must be UNDATED")
    if status == "MISSING_REFERENCE_SEQUENCING_DATE" and kind == "UNDATED":
        _fail(line, "missing reference date cannot be UNDATED")


def derive_timepoint_source(kind, reason, source, status):
    # Mirrors ImportWsiData.deriveTimepointSource.
    if kind == "ESTIMATED":
        return "Verified estimated procedure date relative to first tumor sequencing"
    if kind == "RECORDED":
        return "Recorded procedure date relative to first tumor sequencing"
    return reason or source or status


def normalize_row(row, line):
    """Parse one row like ImportWsiData.normalize and return its metadata object."""
    metadata = {}
    for field in PUBLIC_STRING_FIELDS:
        if row[field]:
            metadata[field.lower()] = row[field]
    reference = row["REFERENCE_SAMPLE_ID"]
    if reference and reference.upper() != "UNMATCHED":
        metadata["reference_sample_id"] = reference
    metadata["is_hne"] = _boolean(row, "IS_HNE", line)
    metadata["is_ihc"] = _boolean(row, "IS_IHC", line)
    can_serve = _boolean(row, "CAN_SERVE_TILES", line)
    metadata["can_serve_tiles"] = can_serve

    file_size = _optional_int(row, "FILE_SIZE_BYTES", line)
    if file_size is not None:
        if file_size < 0:
            _fail(line, "FILE_SIZE_BYTES cannot be negative")
        metadata["file_size_bytes"] = file_size

    thumbnail_width = _optional_int(row, "THUMBNAIL_WIDTH", line)
    thumbnail_height = _optional_int(row, "THUMBNAIL_HEIGHT", line)
    for value in (thumbnail_width, thumbnail_height):
        if value is not None and not 0 <= value <= 0xFFFFFFFF:
            _fail(line, "thumbnail dimension is out of range")
    tile_metadata = None
    if row["TILE_METADATA_JSON"]:
        try:
            tile_metadata = json.loads(row["TILE_METADATA_JSON"])
        except ValueError:
            tile_metadata = None
        if not isinstance(tile_metadata, dict):
            _fail(line, "TILE_METADATA_JSON must be a JSON object")

    serving = {}
    if can_serve:
        for field in ("SOURCE_URL", "TILE_METADATA_JSON", "THUMBNAIL_URL", "THUMBNAIL_CONTENT_TYPE"):
            _require(row, field, line)
        for value in (thumbnail_width, thumbnail_height):
            if value is None or not 1 <= value <= 8192:
                _fail(line, "servable thumbnail dimensions must be between 1 and 8192")
        serving = {
            "source_url": row["SOURCE_URL"],
            "tile_metadata_json": tile_metadata,
            "thumbnail_url": row["THUMBNAIL_URL"],
            "thumbnail_width": thumbnail_width,
            "thumbnail_height": thumbnail_height,
            "thumbnail_content_type": row["THUMBNAIL_CONTENT_TYPE"],
        }
    # Non-servable rows carry no serving fields, as the native importer stored them as null.
    metadata[SERVING_KEY] = serving

    start_days = _optional_int(row, "TIMELINE_START_DAYS", line)
    _validate_timing(start_days, row["TIMELINE_DATE_STATUS"], row["TIMELINE_DATE_KIND"],
                     row["TIMELINE_DATE_SOURCE"], row["TIMELINE_DATE_REASON"],
                     row["TIMELINE_COORDINATE_SYSTEM"], line)
    if start_days is not None:
        metadata["timeline_start_days"] = start_days
    metadata["timepoint_source"] = row["TIMEPOINT_SOURCE"] or derive_timepoint_source(
        row["TIMELINE_DATE_KIND"], row["TIMELINE_DATE_REASON"],
        row["TIMELINE_DATE_SOURCE"], row["TIMELINE_DATE_STATUS"])
    return metadata


def parse_slides(rows):
    """Normalize every row and apply the cross-row checks of ImportWsiData.normalize."""
    slides = []
    image_ids = set()
    patient_references = {}
    parts = {}
    blocks = {}
    for line, row in rows:
        patient_id = _require(row, "PATIENT_ID", line)
        image_id = _require(row, "IMAGE_ID", line)
        if image_id in image_ids:
            _fail(line, f"IMAGE_ID is not unique: {image_id}")
        image_ids.add(image_id)
        part_key = _require(row, "PART_KEY", line)
        block_key = _require(row, "BLOCK_KEY", line)
        if "?" in part_key or "?" in block_key:
            _fail(line, "part/block keys cannot contain ?")
        match_level = row["MATCH_LEVEL"]
        if match_level not in MATCH_LEVELS:
            _fail(line, "invalid MATCH_LEVEL")
        sample_id = row["SAMPLE_ID"]
        if match_level == "UNMATCHED" and sample_id:
            _fail(line, "UNMATCHED rows cannot have SAMPLE_ID")
        if match_level != "UNMATCHED" and not sample_id:
            _fail(line, "matched rows require SAMPLE_ID")
        reference = row["REFERENCE_SAMPLE_ID"]
        reference = reference if reference and reference.upper() != "UNMATCHED" else None
        if patient_id in patient_references and patient_references[patient_id] != reference:
            _fail(line, "patient has conflicting reference samples")
        patient_references[patient_id] = reference
        part = tuple(row[field] for field in (
            "PART_NUMBER", "PART_DESIGNATOR", "PART_TYPE",
            "PART_DESCRIPTION", "SUBSPECIALTY", "PATH_DX_TITLE"))
        if parts.setdefault((patient_id, part_key), part) != part:
            _fail(line, "conflicting part metadata")
        block = (row["BLOCK_NUMBER"], row["BLOCK_LABEL"])
        if blocks.setdefault((patient_id, part_key, block_key), block) != block:
            _fail(line, "conflicting block metadata")
        _require(row, "SPECIMEN_KEY", line)
        metadata = normalize_row(row, line)
        slides.append({
            "patient_id": patient_id,
            "sample_id": sample_id or None,
            "image_id": image_id,
            "match_level": match_level,
            "metadata": metadata,
        })
    return slides


def count_slides(slides):
    """Per-entity counts with the semantics of ImportWsiData.insertSampleSlideCounts.

    One count per IMAGE_ID. Sample counts cover matched slides only, so samples
    without a matched slide get no row; patient counts include unmatched slides,
    so every patient with a slide gets a row. Part/block counts follow
    MATCH_LEVEL and zeros are written for entities that have a row. Slides that
    cannot serve tiles are counted.
    """
    by_sample = {}
    by_patient = {}
    for slide in slides:
        targets = [by_patient.setdefault(slide["patient_id"], [0, 0, 0])]
        if slide["sample_id"] is not None:
            targets.append(by_sample.setdefault((slide["patient_id"], slide["sample_id"]), [0, 0, 0]))
        for counts in targets:
            counts[0] += 1
            if slide["match_level"] == "PART":
                counts[1] += 1
            elif slide["match_level"] == "BLOCK":
                counts[2] += 1
    return by_sample, by_patient


def _check_cell(value, file_name):
    text = "" if value is None else str(value)
    if any(character in text for character in "\t\n\r"):
        raise ConversionError(
            f"{file_name}: value contains a tab or line break and cannot be written as TSV: {text!r}")
    return text


def write_tsv(path, rows):
    """Write raw tab-separated rows; no quoting, one physical line per record."""
    lines = ["\t".join(_check_cell(value, path.name) for value in row) for row in rows]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_meta(path, entries):
    path.write_text("".join(f"{key}: {value}\n" for key, value in entries), encoding="utf-8")


def _clinical_header_rows(identifier_columns, attributes):
    names = [name for name, _, _ in identifier_columns] + [attribute[1] for attribute in attributes]
    descriptions = [desc for _, desc, _ in identifier_columns] + [attribute[2] for attribute in attributes]
    datatypes = ["STRING"] * len(identifier_columns) + ["NUMBER"] * len(attributes)
    priorities = ["1"] * (len(identifier_columns) + len(attributes))
    columns = [column for _, _, column in identifier_columns] + [attribute[0] for attribute in attributes]
    return [
        ["#" + names[0]] + names[1:],
        ["#" + descriptions[0]] + descriptions[1:],
        ["#" + datatypes[0]] + datatypes[1:],
        ["#" + priorities[0]] + priorities[1:],
        columns,
    ]


def _data_header(path):
    """Return the column header (first non-comment line) of a tab-delimited file."""
    try:
        with open(path, encoding="utf-8", errors="replace") as stream:
            for line in stream:
                if line.startswith("#") or not line.strip():
                    continue
                return [column.strip() for column in line.rstrip("\r\n").split("\t")]
    except OSError:
        return []
    return []


def check_study_conflicts(study_dir, own_outputs):
    """Refuse to emit files that would duplicate definitions already in the study.

    ``own_outputs`` are resolved paths the converter is about to (over)write;
    they are ignored so the converter can be re-run into the same directory.
    """
    study_dir = Path(study_dir)
    if not study_dir.is_dir():
        raise ConversionError(f"--study-dir is not a directory: {study_dir}")
    problems = []
    clinical_files = set(study_dir.glob("data_clinical*"))
    resource_meta = []
    for meta_path in sorted(study_dir.iterdir()):
        if not meta_path.is_file() or "meta" not in meta_path.name.lower() or meta_path.resolve() in own_outputs:
            continue
        try:
            meta = read_meta(meta_path)
        except ConversionError:
            continue
        if meta.get("genetic_alteration_type") == "CLINICAL" and meta.get("data_filename"):
            clinical_files.add(study_dir / meta["data_filename"])
        if meta.get("resource_type") in ("DEFINITION", "SAMPLE", "PATIENT"):
            resource_meta.append((meta_path.name, meta["resource_type"]))
    for path in sorted(clinical_files):
        if path.resolve() in own_outputs:
            continue
        duplicated = sorted(COUNT_ATTRIBUTE_IDS.intersection(_data_header(path)))
        if duplicated:
            problems.append(f"{path.name} already defines {', '.join(duplicated)}")
    for name, resource_type in resource_meta:
        problems.append(
            f"{name} already provides a {resource_type} resource file; a study may contain only one, "
            f"so merge the converted rows into it instead")
    if problems:
        raise ConversionError(
            "the study directory already contains data the converter would duplicate:\n  - "
            + "\n  - ".join(problems)
            + "\nRemove the WSI count columns from those files (the converted counts replace them) "
              "or merge the converted files by hand.")


def convert(meta_wsi, output_dir, portal_base_url, study_dir=None):
    """Convert a legacy WSI pair; return the list of files written."""
    meta_wsi = Path(meta_wsi)
    output_dir = Path(output_dir)
    base_url = normalize_base_url(portal_base_url)
    meta = read_wsi_meta(meta_wsi)
    study_id = meta["cancer_study_identifier"]
    data_path = meta_wsi.parent / meta["data_filename"]
    try:
        slides = parse_slides(read_rows(data_path))
    except ConversionError as error:
        raise ConversionError(f"{data_path}: {error}") from error

    sample_rows, patient_rows = [], []
    for slide in slides:
        url = viewer_url(base_url, study_id, slide["patient_id"], slide["image_id"])
        metadata = json.dumps(slide["metadata"], separators=(",", ":"), sort_keys=True, ensure_ascii=True)
        if slide["sample_id"] is not None:
            sample_rows.append([slide["patient_id"], slide["sample_id"], SAMPLE_RESOURCE_ID, url,
                                slide["image_id"], RESOURCE_TYPE, metadata])
        else:
            patient_rows.append([slide["patient_id"], PATIENT_RESOURCE_ID, url,
                                 slide["image_id"], RESOURCE_TYPE, metadata])
    by_sample, by_patient = count_slides(slides)

    outputs = []  # (data file, rows, meta file, meta entries)
    definitions = []
    if sample_rows:
        definitions.append([SAMPLE_RESOURCE_ID, "Pathology slides",
                            "Whole-slide images matched to a sample", "SAMPLE", "FALSE", "1"])
    if patient_rows:
        definitions.append([PATIENT_RESOURCE_ID, "Pathology slides",
                            "Whole-slide images not matched to a sample", "PATIENT", "FALSE", "1"])
    outputs.append((DEFINITION_FILE,
                    [["RESOURCE_ID", "DISPLAY_NAME", "DESCRIPTION", "RESOURCE_TYPE",
                      "OPEN_BY_DEFAULT", "PRIORITY"]] + definitions,
                    "meta_resource_definition.txt",
                    [("cancer_study_identifier", study_id), ("resource_type", "DEFINITION")]))
    if sample_rows:
        outputs.append((SAMPLE_RESOURCE_FILE, [RESOURCE_HEADER_SAMPLE] + sample_rows,
                        "meta_resource_sample.txt",
                        [("cancer_study_identifier", study_id), ("resource_type", "SAMPLE")]))
    if patient_rows:
        outputs.append((PATIENT_RESOURCE_FILE, [RESOURCE_HEADER_PATIENT] + patient_rows,
                        "meta_resource_patient.txt",
                        [("cancer_study_identifier", study_id), ("resource_type", "PATIENT")]))
    if by_sample:
        outputs.append((SAMPLE_COUNTS_FILE,
                        _clinical_header_rows(
                            [("Patient Identifier", "Patient identifier", "PATIENT_ID"),
                             ("Sample Identifier", "Sample identifier", "SAMPLE_ID")],
                            SAMPLE_COUNT_ATTRIBUTES)
                        + [[patient, sample] + [str(count) for count in counts]
                           for (patient, sample), counts in by_sample.items()],
                        "meta_clinical_sample_wsi_counts.txt",
                        [("cancer_study_identifier", study_id),
                         ("genetic_alteration_type", "CLINICAL"),
                         ("datatype", "SAMPLE_ATTRIBUTES")]))
    outputs.append((PATIENT_COUNTS_FILE,
                    _clinical_header_rows(
                        [("Patient Identifier", "Patient identifier", "PATIENT_ID")],
                        PATIENT_COUNT_ATTRIBUTES)
                    + [[patient] + [str(count) for count in counts]
                       for patient, counts in by_patient.items()],
                    "meta_clinical_patient_wsi_counts.txt",
                    [("cancer_study_identifier", study_id),
                     ("genetic_alteration_type", "CLINICAL"),
                     ("datatype", "PATIENT_ATTRIBUTES")]))

    own_outputs = {(output_dir / name).resolve()
                   for data_file, _, meta_file, _ in outputs for name in (data_file, meta_file)}
    if study_dir is not None:
        check_study_conflicts(study_dir, own_outputs)

    output_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for data_file, rows, meta_file, meta_entries in outputs:
        write_tsv(output_dir / data_file, rows)
        write_meta(output_dir / meta_file, meta_entries + [("data_filename", data_file)])
        written.extend([output_dir / meta_file, output_dir / data_file])
    return written


def interface(args=None):
    parser = argparse.ArgumentParser(
        description="Convert a legacy format-v3 meta_wsi/data_wsi pair into standard resource "
                    "and clinical slide-count files. Runs offline.")
    parser.add_argument("--meta-wsi", type=Path, required=True,
                        help="path to the legacy meta_wsi.txt (its data_filename is read next to it)")
    parser.add_argument("--output-dir", type=Path, required=True,
                        help="directory to write the converted meta/data files to")
    parser.add_argument("--portal-base-url", required=True,
                        help="absolute http(s) URL of the portal, including any context path "
                             "(e.g. https://example.org/cbioportal); used for viewer links")
    parser.add_argument("--study-dir", type=Path,
                        help="study directory to check for clinical or resource files the output "
                             "would duplicate")
    return parser.parse_args(args)


def main(args=None):
    parsed = interface(args)
    try:
        written = convert(parsed.meta_wsi, parsed.output_dir, parsed.portal_base_url, parsed.study_dir)
    except ConversionError as error:
        print(f"convertWsiToResources.py: error: {error}", file=sys.stderr)
        return 1
    for path in written:
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
