/*
 * Copyright (c) 2026 Memorial Sloan-Kettering Cancer Center.
 *
 * This file is part of cBioPortal and is licensed under the AGPL.
 */

package org.mskcc.cbio.portal.scripts;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import joptsimple.OptionSet;
import org.mskcc.cbio.portal.dao.ClickHouseBulkLoader;
import org.mskcc.cbio.portal.dao.DaoException;
import org.mskcc.cbio.portal.dao.JdbcUtil;
import org.mskcc.cbio.portal.util.ConsoleUtil;

import java.io.BufferedReader;
import java.io.File;
import java.io.FileReader;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.sql.Timestamp;
import java.time.Instant;
import java.util.ArrayList;
import java.util.Arrays;
import java.util.HashMap;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.List;
import java.util.Map;
import java.util.Properties;
import java.util.Set;
import java.util.UUID;

/**
 * Imports the canonical PATHOLOGY_SLIDES/WSI study files into ClickHouse.
 *
 * The complete input is parsed and resolved before any rows are written. Child
 * rows are then bulk-loaded and the release manifest is inserted last; the
 * manifest is therefore the visibility boundary for a replacement snapshot.
 */
public class ImportWsiData extends ConsoleRunnable {

    private static final int COLUMN_COUNT = 33;
    private static final String[] COLUMNS = {
        "PATIENT_ID", "REFERENCE_SAMPLE_ID", "SAMPLE_ID", "IMAGE_ID",
        "PART_KEY", "PART_NUMBER", "PART_DESIGNATOR", "PART_TYPE",
        "PART_DESCRIPTION", "SUBSPECIALTY", "PATH_DX_TITLE", "BLOCK_KEY",
        "BLOCK_NUMBER", "BLOCK_LABEL", "MATCH_LEVEL", "SPECIMEN_KEY",
        "STAIN_NAME", "STAIN_GROUP", "IS_HNE", "IS_IHC", "MAGNIFICATION",
        "FILE_SIZE_BYTES", "BARCODE", "SLIDE_TYPE", "PROCEDURE_DATE_DAYS",
        "TIMEPOINT_SOURCE", "CAN_SERVE_TILES", "SOURCE_URL",
        "TILE_METADATA_JSON", "THUMBNAIL_URL", "THUMBNAIL_WIDTH",
        "THUMBNAIL_HEIGHT", "THUMBNAIL_CONTENT_TYPE"
    };

    private static final String[] RELEASE_PATIENT_FIELDS = {
        "cancer_study_id", "release_id", "patient_id", "reference_sample_id"
    };
    private static final String[] PART_FIELDS = {
        "cancer_study_id", "release_id", "patient_id", "part_key",
        "part_number", "part_designator", "part_type", "part_description",
        "subspecialty", "path_dx_title"
    };
    private static final String[] BLOCK_FIELDS = {
        "cancer_study_id", "release_id", "patient_id", "part_key", "block_key",
        "block_number", "block_label"
    };
    private static final String[] SLIDE_FIELDS = {
        "cancer_study_id", "release_id", "patient_id", "image_id", "stain_name",
        "stain_group", "is_hne", "is_ihc", "magnification", "file_size_bytes",
        "can_serve_tiles", "barcode", "slide_type", "source_url",
        "tile_metadata_json", "thumbnail_url", "thumbnail_width",
        "thumbnail_height", "thumbnail_content_type"
    };
    private static final String[] PLACEMENT_FIELDS = {
        "cancer_study_id", "release_id", "patient_id", "image_id", "part_key",
        "block_key", "sample_id", "match_level", "specimen_key",
        "procedure_date_days", "timepoint_source"
    };

    private static final ObjectMapper JSON = new ObjectMapper();

    private record SampleRef(long internalId, long patientId) {}
    private record StudyRefs(long studyId, Map<String, Long> patients,
                             Map<String, SampleRef> samples) {}

    private static final class ImportRows {
        private final Map<String, String[]> patients = new LinkedHashMap<>();
        private final Map<String, String[]> parts = new LinkedHashMap<>();
        private final Map<String, String[]> blocks = new LinkedHashMap<>();
        private final Map<String, String[]> slides = new LinkedHashMap<>();
        private final Map<String, String[]> placements = new LinkedHashMap<>();
    }

    private static String value(String[] fields, int index) {
        return fields[index].trim();
    }

    private static String nullable(String value) {
        return value == null || value.isBlank() ? null : value;
    }

    private static long requiredLong(String value, String field, int line) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Line " + line + ": " + field + " is required");
        }
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException exception) {
            throw new IllegalArgumentException("Line " + line + ": invalid " + field, exception);
        }
    }

    private static Long optionalLong(String value, String field, int line) {
        if (value == null || value.isBlank()) {
            return null;
        }
        try {
            return Long.parseLong(value);
        } catch (NumberFormatException exception) {
            throw new IllegalArgumentException("Line " + line + ": invalid " + field, exception);
        }
    }

    private static boolean requiredBoolean(String value, String field, int line) {
        if (!"TRUE".equals(value) && !"FALSE".equals(value)) {
            throw new IllegalArgumentException("Line " + line + ": " + field + " must be TRUE or FALSE");
        }
        return "TRUE".equals(value);
    }

    private static void require(String value, String field, int line) {
        if (value == null || value.isBlank()) {
            throw new IllegalArgumentException("Line " + line + ": " + field + " is required");
        }
    }

    private static void requireAbsoluteUrl(String value, String field, int line) {
        if (value == null || value.isBlank()) {
            return;
        }
        try {
            var uri = java.net.URI.create(value);
            if (uri.getScheme() == null || (uri.getHost() == null && uri.getPath() == null)) {
                throw new IllegalArgumentException();
            }
        } catch (IllegalArgumentException exception) {
            throw new IllegalArgumentException("Line " + line + ": " + field + " must be an absolute URL");
        }
    }

    private static void requireJsonObject(String value, int line) {
        if (value == null || value.isBlank()) {
            return;
        }
        try {
            JsonNode node = JSON.readTree(value);
            if (node == null || !node.isObject()) {
                throw new IllegalArgumentException();
            }
        } catch (IOException | IllegalArgumentException exception) {
            throw new IllegalArgumentException("Line " + line + ": TILE_METADATA_JSON must be a JSON object");
        }
    }

    private static Properties readMetadata(String metadataFile) throws IOException {
        Properties properties = new TrimmedProperties();
        try (FileReader reader = new FileReader(metadataFile, StandardCharsets.UTF_8)) {
            properties.load(reader);
        }
        if (!"PATHOLOGY_SLIDES".equals(properties.getProperty("genetic_alteration_type"))
            || !"WSI".equals(properties.getProperty("datatype"))) {
            throw new IllegalArgumentException("WSI metadata must use PATHOLOGY_SLIDES / WSI");
        }
        if (!"1".equals(properties.getProperty("format_version"))) {
            throw new IllegalArgumentException("Unsupported WSI format_version; expected 1");
        }
        for (String field : List.of("cancer_study_identifier", "data_filename")) {
            require(properties.getProperty(field), field, 0);
        }
        return properties;
    }

    private static List<String[]> readRows(String dataFile) throws IOException {
        List<String[]> rows = new ArrayList<>();
        try (BufferedReader reader = new BufferedReader(new FileReader(dataFile, StandardCharsets.UTF_8))) {
            for (int i = 1; i <= 4; i++) {
                String comment = reader.readLine();
                if (comment == null || !comment.startsWith("#")) {
                    throw new IllegalArgumentException("WSI data must begin with four comment rows");
                }
            }
            String header = reader.readLine();
            if (header != null && header.endsWith("\r")) {
                header = header.substring(0, header.length() - 1);
            }
            if (header == null || !Arrays.equals(COLUMNS, header.split("\\t", -1))) {
                throw new IllegalArgumentException("WSI data has an invalid header or column order");
            }
            String line;
            int lineNumber = 5;
            while ((line = reader.readLine()) != null) {
                lineNumber++;
                if (line.endsWith("\r")) {
                    line = line.substring(0, line.length() - 1);
                }
                String[] fields = line.split("\\t", -1);
                if (fields.length != COLUMN_COUNT) {
                    throw new IllegalArgumentException("Line " + lineNumber + ": expected "
                        + COLUMN_COUNT + " columns, found " + fields.length);
                }
                if (Arrays.stream(fields).allMatch(String::isBlank)) {
                    throw new IllegalArgumentException("Line " + lineNumber + ": blank WSI row");
                }
                rows.add(fields);
            }
        }
        if (rows.isEmpty()) {
            throw new IllegalArgumentException("WSI data file contains no slide rows");
        }
        return rows;
    }

    private static StudyRefs resolveReferences(Properties metadata) throws SQLException {
        Connection connection = null;
        try {
            connection = JdbcUtil.getDbConnection(ImportWsiData.class);
            long studyId;
            try (PreparedStatement statement = connection.prepareStatement(
                "SELECT cancer_study_id FROM cancer_study WHERE cancer_study_identifier = ?")) {
                statement.setString(1, metadata.getProperty("cancer_study_identifier"));
                try (ResultSet result = statement.executeQuery()) {
                    if (!result.next()) {
                        throw new IllegalArgumentException("Cancer study not found: "
                            + metadata.getProperty("cancer_study_identifier"));
                    }
                    studyId = result.getLong(1);
                }
            }
            Map<String, Long> patients = new HashMap<>();
            try (PreparedStatement statement = connection.prepareStatement(
                "SELECT internal_id, stable_id FROM patient WHERE cancer_study_id = ?")) {
                statement.setLong(1, studyId);
                try (ResultSet result = statement.executeQuery()) {
                    while (result.next()) {
                        patients.put(result.getString("stable_id"), result.getLong("internal_id"));
                    }
                }
            }
            Map<String, SampleRef> samples = new HashMap<>();
            try (PreparedStatement statement = connection.prepareStatement(
                "SELECT s.internal_id, s.stable_id, s.patient_id FROM sample s "
                    + "INNER JOIN patient p ON p.internal_id = s.patient_id "
                    + "WHERE p.cancer_study_id = ?")) {
                statement.setLong(1, studyId);
                try (ResultSet result = statement.executeQuery()) {
                    while (result.next()) {
                        samples.put(result.getString("stable_id"),
                            new SampleRef(result.getLong("internal_id"), result.getLong("patient_id")));
                    }
                }
            }
            return new StudyRefs(studyId, patients, samples);
        } finally {
            JdbcUtil.closeAll(ImportWsiData.class, connection, null, null);
        }
    }

    private static ImportRows normalize(List<String[]> input, StudyRefs refs, String releaseId) {
        ImportRows output = new ImportRows();
        Set<String> imageIds = new HashSet<>();
        Map<String, Long> patientReferences = new HashMap<>();
        for (int rowIndex = 0; rowIndex < input.size(); rowIndex++) {
            int line = rowIndex + 6;
            String[] fields = input.get(rowIndex);
            String patientStableId = value(fields, 0);
            String patientKey = patientStableId;
            long patientId = refs.patients.getOrDefault(patientStableId, -1L);
            if (patientId < 0) {
                throw new IllegalArgumentException("Line " + line + ": patient not found: " + patientStableId);
            }
            String imageId = value(fields, 3);
            require(imageId, "IMAGE_ID", line);
            if (!imageIds.add(imageId)) {
                throw new IllegalArgumentException("Line " + line + ": IMAGE_ID is not unique: " + imageId);
            }
            String partKey = value(fields, 4);
            String blockKey = value(fields, 11);
            require(partKey, "PART_KEY", line);
            require(blockKey, "BLOCK_KEY", line);
            if (partKey.contains("?") || blockKey.contains("?")) {
                throw new IllegalArgumentException("Line " + line + ": part/block keys cannot contain ?");
            }
            String matchLevel = value(fields, 14);
            if (!Set.of("BLOCK", "PART", "UNMATCHED").contains(matchLevel)) {
                throw new IllegalArgumentException("Line " + line + ": invalid MATCH_LEVEL");
            }
            String sampleStableId = value(fields, 2);
            if ("UNMATCHED".equals(matchLevel) && !sampleStableId.isBlank()) {
                throw new IllegalArgumentException("Line " + line + ": UNMATCHED rows cannot have SAMPLE_ID");
            }
            if (!"UNMATCHED".equals(matchLevel) && sampleStableId.isBlank()) {
                throw new IllegalArgumentException("Line " + line + ": matched rows require SAMPLE_ID");
            }
            SampleRef sample = null;
            if (!sampleStableId.isBlank()) {
                sample = refs.samples.get(sampleStableId);
                if (sample == null || sample.patientId() != patientId) {
                    throw new IllegalArgumentException("Line " + line + ": sample does not belong to patient");
                }
            }
            String referenceStableId = value(fields, 1);
            Long referenceSampleId = null;
            if (!referenceStableId.isBlank() && !"UNMATCHED".equalsIgnoreCase(referenceStableId)) {
                SampleRef reference = refs.samples.get(referenceStableId);
                if (reference == null || reference.patientId() != patientId) {
                    throw new IllegalArgumentException("Line " + line + ": reference sample does not belong to patient");
                }
                referenceSampleId = reference.internalId();
            }
            if (patientReferences.containsKey(patientKey)
                && !java.util.Objects.equals(patientReferences.get(patientKey), referenceSampleId)) {
                throw new IllegalArgumentException("Line " + line + ": patient has conflicting reference samples");
            }
            patientReferences.put(patientKey, referenceSampleId);
            output.patients.putIfAbsent(patientKey, new String[] {
                Long.toString(refs.studyId()), releaseId, Long.toString(patientId), nullableLong(referenceSampleId)
            });

            String partMapKey = patientKey + "\u0000" + partKey;
            String[] part = new String[] {
                Long.toString(refs.studyId()), releaseId, Long.toString(patientId), partKey,
                nullable(value(fields, 5)), nullable(value(fields, 6)), nullable(value(fields, 7)),
                nullable(value(fields, 8)), nullable(value(fields, 9)), nullable(value(fields, 10))
            };
            putConsistent(output.parts, partMapKey, part, line, "part");

            String blockMapKey = partMapKey + "\u0000" + blockKey;
            String[] block = new String[] {
                Long.toString(refs.studyId()), releaseId, Long.toString(patientId), partKey, blockKey,
                nullable(value(fields, 12)), nullable(value(fields, 13))
            };
            putConsistent(output.blocks, blockMapKey, block, line, "block");

            boolean isHne = requiredBoolean(value(fields, 18), "IS_HNE", line);
            boolean isIhc = requiredBoolean(value(fields, 19), "IS_IHC", line);
            boolean canServe = requiredBoolean(value(fields, 26), "CAN_SERVE_TILES", line);
            Long fileSize = optionalLong(value(fields, 21), "FILE_SIZE_BYTES", line);
            if (fileSize != null && fileSize < 0) {
                throw new IllegalArgumentException("Line " + line + ": FILE_SIZE_BYTES cannot be negative");
            }
            String sourceUrl = nullable(value(fields, 27));
            String tileMetadata = nullable(value(fields, 28));
            String thumbnailUrl = nullable(value(fields, 29));
            String thumbnailContentType = nullable(value(fields, 32));
            Long thumbnailWidth = optionalLong(value(fields, 30), "THUMBNAIL_WIDTH", line);
            Long thumbnailHeight = optionalLong(value(fields, 31), "THUMBNAIL_HEIGHT", line);
            Long procedureDateDays = optionalLong(value(fields, 24), "PROCEDURE_DATE_DAYS", line);
            if (procedureDateDays != null
                && (procedureDateDays < Integer.MIN_VALUE || procedureDateDays > Integer.MAX_VALUE)) {
                throw new IllegalArgumentException("Line " + line + ": PROCEDURE_DATE_DAYS is out of range");
            }
            if ((thumbnailWidth != null && (thumbnailWidth < 0 || thumbnailWidth > 0xffffffffL))
                || (thumbnailHeight != null && (thumbnailHeight < 0 || thumbnailHeight > 0xffffffffL))) {
                throw new IllegalArgumentException("Line " + line + ": thumbnail dimension is out of range");
            }
            requireAbsoluteUrl(sourceUrl, "SOURCE_URL", line);
            requireAbsoluteUrl(thumbnailUrl, "THUMBNAIL_URL", line);
            requireJsonObject(tileMetadata, line);
            if (canServe) {
                require(sourceUrl, "SOURCE_URL", line);
                require(tileMetadata, "TILE_METADATA_JSON", line);
                require(thumbnailUrl, "THUMBNAIL_URL", line);
                require(value(fields, 32), "THUMBNAIL_CONTENT_TYPE", line);
                if (thumbnailWidth == null || thumbnailWidth <= 0 || thumbnailHeight == null || thumbnailHeight <= 0) {
                    throw new IllegalArgumentException("Line " + line + ": servable thumbnails require positive dimensions");
                }
            } else {
                sourceUrl = null;
                tileMetadata = null;
                thumbnailUrl = null;
                thumbnailWidth = null;
                thumbnailHeight = null;
                thumbnailContentType = null;
            }
            String[] slide = new String[] {
                Long.toString(refs.studyId()), releaseId, Long.toString(patientId), imageId,
                nullable(value(fields, 16)), nullable(value(fields, 17)), isHne ? "1" : "0",
                isIhc ? "1" : "0", nullable(value(fields, 20)), nullableLong(fileSize),
                canServe ? "1" : "0", nullable(value(fields, 22)), nullable(value(fields, 23)),
                sourceUrl, tileMetadata, thumbnailUrl, nullableLong(thumbnailWidth),
                nullableLong(thumbnailHeight), thumbnailContentType
            };
            output.slides.put(imageId, slide);

            String[] placement = new String[] {
                Long.toString(refs.studyId()), releaseId, Long.toString(patientId), imageId, partKey,
                blockKey, sample == null ? null : Long.toString(sample.internalId()), matchLevel,
                value(fields, 15), nullableLong(procedureDateDays),
                nullable(value(fields, 25))
            };
            output.placements.put(imageId, placement);
        }
        return output;
    }

    private static String nullableLong(Long value) {
        return value == null ? null : Long.toString(value);
    }

    private static void putConsistent(Map<String, String[]> values, String key, String[] value,
                                      int line, String name) {
        String[] previous = values.putIfAbsent(key, value);
        if (previous != null && !Arrays.equals(previous, value)) {
            throw new IllegalArgumentException("Line " + line + ": conflicting " + name + " metadata");
        }
    }

    private static void insertRows(ImportRows rows) throws DaoException {
        ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_release_patient").setFieldNames(RELEASE_PATIENT_FIELDS);
        rows.patients.values().forEach(record -> ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_release_patient").insertRecord(record));
        ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_part").setFieldNames(PART_FIELDS);
        rows.parts.values().forEach(record -> ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_part").insertRecord(record));
        ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_block").setFieldNames(BLOCK_FIELDS);
        rows.blocks.values().forEach(record -> ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_block").insertRecord(record));
        ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_slide").setFieldNames(SLIDE_FIELDS);
        rows.slides.values().forEach(record -> ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_slide").insertRecord(record));
        ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_slide_placement").setFieldNames(PLACEMENT_FIELDS);
        rows.placements.values().forEach(record -> ClickHouseBulkLoader.getClickHouseBulkLoader("wsi_slide_placement").insertRecord(record));
        ClickHouseBulkLoader.flushAll();
    }

    private static void insertRelease(long studyId, String releaseId, long releaseVersion, Instant releasedAt)
        throws SQLException {
        Connection connection = null;
        try {
            connection = JdbcUtil.getDbConnection(ImportWsiData.class);
            try (PreparedStatement statement = connection.prepareStatement(
                "INSERT INTO wsi_release (cancer_study_id, release_id, release_version, released_at) VALUES (?, ?, ?, ?)")) {
                statement.setLong(1, studyId);
                statement.setString(2, releaseId);
                statement.setLong(3, releaseVersion);
                statement.setTimestamp(4, Timestamp.from(releasedAt));
                statement.executeUpdate();
            }
        } finally {
            JdbcUtil.closeAll(ImportWsiData.class, connection, null, null);
        }
    }

    private static void importData(String metadataFile, String dataFile)
        throws IOException, SQLException, DaoException {
        Properties metadata = readMetadata(metadataFile);
        String expectedData = new File(new File(metadataFile).getParentFile(), metadata.getProperty("data_filename"))
            .getCanonicalPath();
        if (!new File(dataFile).getCanonicalPath().equals(expectedData)) {
            throw new IllegalArgumentException("data filename does not match meta_wsi.txt");
        }
        List<String[]> rows = readRows(dataFile);
        StudyRefs references = resolveReferences(metadata);
        Instant releasedAt = Instant.now();
        long releaseVersion = releasedAt.getEpochSecond() * 1_000_000L + releasedAt.getNano() / 1_000L;
        String releaseId = String.format("%020d-%s", releaseVersion, UUID.randomUUID().toString().replace("-", ""));
        ImportRows normalized = normalize(rows, references, releaseId);

        ClickHouseBulkLoader.bulkLoadOn();
        try {
            insertRows(normalized);
            insertRelease(references.studyId(), releaseId, releaseVersion, releasedAt);
        } finally {
            ClickHouseBulkLoader.bulkLoadOff();
        }
    }

    @Override
    public void run() {
        String description = "Import PATHOLOGY_SLIDES/WSI data";
        OptionSet options = ConsoleUtil.parseStandardDataAndMetaOptions(args, description, true);
        String loadMode = options.has("loadMode") ? (String) options.valueOf("loadMode") : "bulkLoad";
        if (!"bulkLoad".equalsIgnoreCase(loadMode)) {
            throw new UnsupportedOperationException("WSI importer supports bulkLoad load mode only");
        }
        try {
            importData((String) options.valueOf("meta"), (String) options.valueOf("data"));
        } catch (IOException | SQLException | DaoException exception) {
            throw new RuntimeException(exception);
        }
    }

    public ImportWsiData(String[] args) {
        super(args);
    }

    public static void main(String[] args) {
        new ImportWsiData(args).runInConsole();
    }
}
