package org.mskcc.cbio.portal.dao;

import com.fasterxml.jackson.databind.JsonNode;
import com.fasterxml.jackson.databind.ObjectMapper;
import org.junit.Test;

import java.nio.file.Files;
import java.nio.file.Paths;
import java.util.HashMap;
import java.util.List;
import java.util.Map;

import static org.junit.Assert.*;

/**
 * The study slide table's metadata keys against the WSI CUSTOM_METADATA contract that
 * scripts/importer/convertWsiToResources.py declares. The fixture read here is the converter's
 * own output (tests/unit_tests_convert_wsi.py checks it is current), so a key renamed or dropped
 * from the contract fails here instead of silently emptying a slide table column.
 */
public class DaoResourceDataSlideTableKeysTest {

    private static final String DEFINITIONS = "src/test/resources/wsi_resources/data_resource_definition.txt";

    @Test
    public void slideTableKeysAreDeclaredFilterableContractFields() throws Exception {
        List<String> lines = Files.readAllLines(Paths.get(DEFINITIONS));
        List<String> header = List.of(lines.get(0).split("\t", -1));
        int resourceIdColumn = header.indexOf("RESOURCE_ID");
        int customMetadataColumn = header.indexOf("CUSTOM_METADATA");
        Map<String, JsonNode> contracts = new HashMap<>();
        for (String line : lines.subList(1, lines.size())) {
            String[] cells = line.split("\t", -1);
            contracts.put(cells[resourceIdColumn], new ObjectMapper().readTree(cells[customMetadataColumn]));
        }

        for (String resourceId : List.of(DaoResourceData.STUDY_SLIDE_TABLE_RESOURCE_ID,
                DaoResourceData.UNMATCHED_SLIDE_RESOURCE_ID)) {
            assertTrue(resourceId, contracts.containsKey(resourceId));
            Map<String, JsonNode> fields = new HashMap<>();
            contracts.get(resourceId).get("fields").forEach(field -> fields.put(field.get("key").asText(), field));
            for (String key : DaoResourceData.STUDY_SLIDE_TABLE_METADATA_KEYS) {
                // Filterable is the contract's mark for a non-identifying, non-free-text key.
                assertTrue(resourceId + " declares " + key, fields.containsKey(key));
                assertTrue(resourceId + " " + key + " is filterable", fields.get(key).get("filterable").asBoolean());
            }
        }
    }
}
