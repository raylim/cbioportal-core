# WSI study import

cBioPortal core is the sole database writer for whole-slide-image (WSI)
studies. The Databricks/export pipeline produces a normal study directory with
`meta_wsi.txt` and `data_wsi.txt`; the tile server only serves the source and
thumbnail artifacts returned by cBioPortal.

## Upstream artifact publication

Before the study files are exported, a separate scheduled thumbnail process
must read eligible slide inventory/source rows, generate master thumbnails,
write them to the S3/Dell ECS-compatible object store, and populate
`cdsi_prod.pathology_data_mining.slide_thumbnail_registry`. Each successful
registry row carries `artifact_uri`, `tile_metadata_json`, `width`, `height`,
and `content_type`. The Databricks canonical-association query joins the latest
successful registry row, computes `can_serve_tiles`, and exports the artifact
fields below into `data_wsi.txt`.

The scheduled thumbnail process is outside cBioPortal core and outside the
frontend. The frontend is a read-only consumer. The tile-server
`app/thumbnail_worker.py` on-demand CLI may write an object-store JPEG for
development or controlled remediation, but it does not update the registry and
must not be used as the production publication mechanism. A canonical refresh
must wait for the thumbnail batch completion watermark, and successful legacy
rows missing `tile_metadata_json` must be regenerated before export.

## Metadata

```text
cancer_study_identifier: <study stable id>
genetic_alteration_type: PATHOLOGY_SLIDES
datatype: WSI
data_filename: data_wsi.txt
format_version: 1
```

The importer rejects unsupported format versions. A study may contain one WSI
pair. The data file follows the normal cBioPortal five-row preamble: four
comment rows, followed by this exact header:

```text
PATIENT_ID  REFERENCE_SAMPLE_ID  SAMPLE_ID  IMAGE_ID  PART_KEY  PART_NUMBER  PART_DESIGNATOR  PART_TYPE  PART_DESCRIPTION  SUBSPECIALTY  PATH_DX_TITLE  BLOCK_KEY  BLOCK_NUMBER  BLOCK_LABEL  MATCH_LEVEL  SPECIMEN_KEY  STAIN_NAME  STAIN_GROUP  IS_HNE  IS_IHC  MAGNIFICATION  FILE_SIZE_BYTES  BARCODE  SLIDE_TYPE  PROCEDURE_DATE_DAYS  TIMEPOINT_SOURCE  CAN_SERVE_TILES  SOURCE_URL  TILE_METADATA_JSON  THUMBNAIL_URL  THUMBNAIL_WIDTH  THUMBNAIL_HEIGHT  THUMBNAIL_CONTENT_TYPE
```

Values are tab-delimited. Required values are `PATIENT_ID`, `IMAGE_ID`,
`PART_KEY`, `BLOCK_KEY`, `MATCH_LEVEL`, `SPECIMEN_KEY`, `IS_HNE`, `IS_IHC`,
and `CAN_SERVE_TILES`. `MATCH_LEVEL` is `BLOCK`, `PART`, or `UNMATCHED`;
matched rows require `SAMPLE_ID`, while unmatched rows leave it blank.

`IMAGE_ID` is unique within a study. Repeated part and block keys must carry
the same descriptive values. Stable patient, sample, and reference-sample IDs
must resolve to the study, and a sample/reference sample must belong to the
row's patient. `TILE_METADATA_JSON` must be a JSON object when present. URLs
must be absolute. When `CAN_SERVE_TILES=TRUE`, source URL, tile metadata,
thumbnail URL, positive dimensions, and thumbnail content type are mandatory.
Non-servable rows have those artifact columns stored as null.

The importer assumes these values were already materialized by the upstream
Databricks/export pipeline. It does not discover source slides, generate
thumbnails, read `slide_thumbnail_registry`, or write the object store.

## Import commands

Full study import:

```bash
metaImport.py -s /path/to/study
```

Incremental WSI import:

```bash
metaImport.py -d /path/to/wsi-update
```

The incremental file is a complete replacement snapshot. It is not merged with
or updated in place over the active release. WSI loading runs after clinical
sample definitions and before a full study is marked `AVAILABLE`.

## ClickHouse publication

The importer resolves the stable identifiers to internal IDs and normalizes
the flat file into these tables:

- `wsi_release_patient`
- `wsi_part`
- `wsi_block`
- `wsi_slide`
- `wsi_slide_placement`
- `wsi_release`

All child rows share one generated opaque release ID. The release ID includes
the publication timestamp and a UUID. `release_version` is the UTC publication
timestamp in microseconds and is informational; active-release selection uses
`released_at` and `release_id`. The `wsi_release` row is inserted last and is
the visibility boundary. A failed import therefore cannot publish an incomplete
release. Child rows left by a failed write are invisible and can be cleaned up
operationally.

The six tables are provisioned by the cBioPortal backend schema. Core does not
create or migrate production tables. Study deletion removes the release
manifest and all child rows.

The former tile-server ClickHouse loader is not supported; all WSI loads must
use the standard cBioPortal importer.
