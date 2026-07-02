/*
 * Copyright (c) 2026 Memorial Sloan Kettering Cancer Center.
 *
 * This library is distributed in the hope that it will be useful, but WITHOUT
 * ANY WARRANTY, WITHOUT EVEN THE IMPLIED WARRANTY OF MERCHANTABILITY OR FITNESS
 * FOR A PARTICULAR PURPOSE. The software and documentation provided hereunder
 * is on an "as is" basis, and Memorial Sloan Kettering Cancer Center has no
 * obligations to provide maintenance, support, updates, enhancements or
 * modifications. In no event shall Memorial Sloan Kettering Cancer Center be
 * liable to any party for direct, indirect, special, incidental or
 * consequential damages, including lost profits, arising out of the use of this
 * software and its documentation, even if Memorial Sloan Kettering Cancer
 * Center has been advised of the possibility of such damage.
 */

/*
 * This file is part of cBioPortal.
 *
 * cBioPortal is free software: you can redistribute it and/or modify
 * it under the terms of the GNU Affero General Public License as
 * published by the Free Software Foundation, either version 3 of the
 * License.
 *
 * This program is distributed in the hope that it will be useful,
 * but WITHOUT ANY WARRANTY; without even the implied warranty of
 * MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
 * GNU Affero General Public License for more details.
 *
 * You should have received a copy of the GNU Affero General Public License
 * along with this program.  If not, see <http://www.gnu.org/licenses/>.
 */

package org.mskcc.cbio.portal.dao;

import java.io.ByteArrayInputStream;
import java.io.ByteArrayOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.sql.Connection;
import java.sql.PreparedStatement;
import java.sql.ResultSet;
import java.sql.SQLException;
import java.util.Collection;
import java.util.HashSet;
import java.util.LinkedHashMap;
import java.util.Map;
import java.util.Set;

/**
 * Bulk deleter that buffers IDs in memory, streams them into a ClickHouse staging
 * table via TSVWithNames, and issues a single DELETE ... WHERE id IN (SELECT id FROM
 * staging_table) statement. The staging table is a real MergeTree table (not temporary)
 * so that it persists across HTTP requests on ClickHouse Cloud.
 *
 * Mirrors the structure of ClickHouseBulkLoader.
 */
public class ClickHouseBulkDeleter {

    private static final Map<String, ClickHouseBulkDeleter> BULK_DELETERS = new LinkedHashMap<>();

    private final String targetTable;
    private final String idColumn;
    private final String stagingTable;
    private final Set<Long> pendingIds = new HashSet<>();

    private ClickHouseBulkDeleter(String targetTable, String idColumn) {
        this.targetTable = targetTable;
        this.idColumn = idColumn;
        this.stagingTable = "staging_delete_" + targetTable;
    }

    public static ClickHouseBulkDeleter getBulkDeleter(String targetTable, String idColumn) {
        String key = targetTable + ":" + idColumn;
        return BULK_DELETERS.computeIfAbsent(key, k -> new ClickHouseBulkDeleter(targetTable, idColumn));
    }

    public void addId(long id) {
        pendingIds.add(id);
    }

    public void addIds(Collection<? extends Number> ids) {
        for (Number id : ids) {
            pendingIds.add(id.longValue());
        }
    }

    public static int flushAll() throws DaoException {
        int totalDeleted = 0;
        Connection con = null;
        try {
            con = JdbcUtil.getDbConnection(ClickHouseBulkDeleter.class);
            dropAnyExistingStagingTables(con, false); // drop any leftover tables from previous crash/failure
            createAllStagingTables(con);
            populateAllStagingTables(con);
            totalDeleted = deleteRecordsReferencedInStagingTables(con);
        } catch (SQLException | IOException e) {
            throw new DaoException(e);
        } finally {
            try {
                if (con != null) {
                    dropAnyExistingStagingTables(con, true);
                    JdbcUtil.closeAll(ClickHouseBulkDeleter.class, con, null, null);
                }
            } catch (SQLException se) {
                // exceptions will be ignored during second call to dropAnyExistingStagingTables
            }
            clearAllDeleters();
        }
        return totalDeleted;
    }

    private static void clearAllDeleters() {
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            deleter.pendingIds.clear();
        }
        BULK_DELETERS.clear();
    }

    private static void createAllStagingTables(Connection con) throws SQLException {
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            String createTableStatementString = String.format(
                    "CREATE TABLE %s (id Int64) ENGINE = MergeTree() ORDER BY id",
                    deleter.stagingTable);
            try (PreparedStatement stmt = con.prepareStatement(createTableStatementString)) {
                stmt.executeUpdate();
            }
        }
        // wait for table existence to propagate to replicas (if they exist)
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            String alterTableStatementString = String.format(
                    "ALTER TABLE %s MODIFY COMMENT 'Transient' SETTINGS mutations_sync = 3",
                    deleter.stagingTable);
            try (PreparedStatement stmt = con.prepareStatement(alterTableStatementString)) {
                stmt.executeUpdate();
            }
        }
    }

    private static void populateAllStagingTables(Connection con) throws SQLException, DaoException, IOException {
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            deleter.populateStagingTable(con);
        }
        // wait for inserts to be recognized by all replicas
        // note: this is intentionally done in a second loop in the hope that while values are being inserted
        //     into the second, third, ... tables, the time for processing those operations allows for the values
        //     inserted into the first table to be recognized / become visible in the other replica nodes
        //     running in a Clickhouse cluster (such as with clickhouse.cloud). This may avoid retry cycles.
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            if (! deleter.allReplicasReachedRecordCount(con)) {
                throw new DaoException("Failed to see all replicas reflect delete list inserted into table " + deleter.stagingTable);
            }
        }
    }

    private void populateStagingTable(Connection con) throws SQLException, IOException {
        if (pendingIds.isEmpty()) {
            return;
        }
        // Insert IDs into staging table via TSV stream
        byte[] payload = buildTsvPayload();
        String insertStatementString = String.format(
                "INSERT INTO %s (id) FORMAT TSVWithNames",
                stagingTable);
        try (PreparedStatement stmt = con.prepareStatement(insertStatementString)) {
            stmt.setBinaryStream(1, new ByteArrayInputStream(payload));
            stmt.executeUpdate();
        }
    }

    private boolean allReplicasReachedRecordCount(Connection con) throws SQLException {
        int WAIT_CYCLE_MAX_COUNT = 60;
        int WAIT_CYCLE_PERIOD_SECONDS = 10;
        int WAIT_CYCLE_TOLERATE_EXCEPTION_LIMIT = 6;
        int exceptions_ignored = 0;
        for (int cycle = 0 ; cycle < WAIT_CYCLE_MAX_COUNT ; cycle = cycle + 1) {
            String getRecordCountsString = String.format(
                    "SELECT COUNT() as record_count FROM clusterAllReplicas('default', current_database(), %s)",
                    stagingTable);
            try (PreparedStatement pstmt = con.prepareStatement(getRecordCountsString);
                    ResultSet rs = pstmt.executeQuery()) {
                boolean recordCountWasReached = true;
                while (rs.next()) {
                    int recordCountOnReplica = rs.getInt("record_count");
                    if (recordCountOnReplica != pendingIds.size()) {
                        recordCountWasReached = false;
                        break;
                    }
                }
                if (recordCountWasReached) {
                    return true;
                }
                Thread.sleep(1000 * WAIT_CYCLE_PERIOD_SECONDS);
            } catch (SQLException e) {
                if (exceptions_ignored >= WAIT_CYCLE_TOLERATE_EXCEPTION_LIMIT) {
                    throw e;
                }
                exceptions_ignored = exceptions_ignored + 1;
            } catch (InterruptedException ie) {
                // ignore : ok to go on to next cycle immediately
            }
        }
        return false;
    }

    private static int deleteRecordsReferencedInStagingTables(Connection con) throws SQLException {
        int totalDeleted = 0;
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            totalDeleted += deleter.deleteRecordsReferencedInStagingTable(con);
        }
        return totalDeleted;
    }

    private int deleteRecordsReferencedInStagingTable(Connection con) throws SQLException {
        int records_deleted;
        String statementString = String.format(
                "DELETE FROM %s WHERE %s IN (SELECT id FROM %s)",
                targetTable, idColumn, stagingTable);
        try (PreparedStatement stmt = con.prepareStatement(statementString)) {
            records_deleted = stmt.executeUpdate();
        }
        return records_deleted;
    }

    private static void dropAnyExistingStagingTables(Connection con, boolean ignoreExceptions) throws SQLException {
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            String dropStatementString = String.format(
                "DROP TABLE IF EXISTS %s",
                deleter.stagingTable);
            try (PreparedStatement stmt = con.prepareStatement(dropStatementString)) {
                stmt.executeUpdate();
            } catch (SQLException e) {
                if (! ignoreExceptions) {
                    throw e;
                }
            }
        }
    }

    private byte[] buildTsvPayload() throws IOException {
        try (ByteArrayOutputStream buffer = new ByteArrayOutputStream()) {
            // header row
            buffer.write("id\n".getBytes(StandardCharsets.UTF_8));
            // data rows
            for (Long id : pendingIds) {
                buffer.write((id.toString() + "\n").getBytes(StandardCharsets.UTF_8));
            }
            return buffer.toByteArray();
        }
    }
}
