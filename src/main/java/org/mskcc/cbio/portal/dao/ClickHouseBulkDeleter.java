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
import org.slf4j.Logger;
import org.slf4j.LoggerFactory;

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
    private static final Logger log = LoggerFactory.getLogger(ClickHouseBulkDeleter.class);

    private final String targetTable;
    private final String idColumn;
    private final String stagingTable;
    private final Set<Long> pendingIds = new HashSet<>();

    // --- static methods ---

    public static ClickHouseBulkDeleter getBulkDeleter(String targetTable, String idColumn) {
        String key = String.format("%s:%s", targetTable, idColumn);
        return BULK_DELETERS.computeIfAbsent(key, k -> new ClickHouseBulkDeleter(targetTable, idColumn));
    }

    public static long flushAll() throws DaoException {
        long totalDeleted = 0;
        try {
            dropAnyExistingStagingTables(false); // drop any leftover tables from previous crash/failure
            createAllStagingTables();
            populateAllStagingTables();
            totalDeleted = deleteRecordsReferencedInStagingTables();
        } catch (SQLException | IOException e) {
            throw new DaoException(e);
        } finally {
            try {
                dropAnyExistingStagingTables(true);
            } catch (SQLException se) {
                // will not happen because SQLExceptions will be suppressed during second call
                // to dropAnyExistingStagingTables (leftover tables will be tolerated)
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

    private static void createAllStagingTables() throws SQLException, DaoException {
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            Connection con = JdbcUtil.getDbConnection(ClickHouseBulkDeleter.class);
            String createTableStatementString = String.format(
                    "CREATE TABLE %s (id Int64) ENGINE = MergeTree() ORDER BY id",
                    deleter.stagingTable);
            try (PreparedStatement stmt = con.prepareStatement(createTableStatementString)) {
                stmt.executeUpdate();
            } finally {
                JdbcUtil.closeAll(ClickHouseBulkDeleter.class, con, null, null);
            }
        }
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            final int MAX_RETRY_SECONDS = 600;
            if (! deleter.conditionIsTrueAfterTestWithRetry(deleter::allReplicasReportStagingTableExists, MAX_RETRY_SECONDS)) {
                String exceptionMsgString = String.format(
                        "Failed to see all replicas report existence of staging table %s after creation",
                        deleter.stagingTable);
                throw new DaoException(exceptionMsgString);
            }
        }
    }

    private static void populateAllStagingTables() throws SQLException, DaoException, IOException {
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            deleter.populateStagingTable();
        }
        // wait for inserts to be recognized by all replicas
        // note: this is intentionally done in a second loop in the hope that while values are being inserted
        //     into the second, third, ... tables, the time for processing those operations allows for the values
        //     inserted into the first table to be recognized / become visible in the other replica nodes
        //     running in a Clickhouse cluster (such as with clickhouse.cloud). This may avoid retry cycles.
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            final int MAX_RETRY_SECONDS = 600;
            if (! deleter.conditionIsTrueAfterTestWithRetry(deleter::allReplicasReportExpectedStagingTableRecordCount, MAX_RETRY_SECONDS)) {
                String exceptionMsgString = String.format(
                        "Failed to see all replicas reflect delete list inserted into table %s",
                        deleter.stagingTable);
                throw new DaoException(exceptionMsgString);
            }
        }
    }

    private static long deleteRecordsReferencedInStagingTables() throws SQLException {
        long totalDeleted = 0;
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            totalDeleted += deleter.deleteRecordsReferencedInStagingTable();
        }
        return totalDeleted;
    }

    private static void dropAnyExistingStagingTables(boolean deletionHasBeenExecuted) throws SQLException, DaoException {
        for (ClickHouseBulkDeleter deleter : BULK_DELETERS.values()) {
            // wait until all ids in the stagingTable table have been fully deleted from the target table
            final int MAX_RETRY_SECONDS = 600;
            if (deletionHasBeenExecuted && ! deleter.conditionIsTrueAfterTestWithRetry(deleter::allReplicasCompletedDeletion, MAX_RETRY_SECONDS)) {
                String exceptionMessageString = String.format(
                        "Failed to complete the delete operation on all replicas for table %s",
                        deleter.stagingTable);
                throw new DaoException(exceptionMessageString);
            }
            String dropStatementString = String.format(
                    "DROP TABLE IF EXISTS %s",
                    deleter.stagingTable);
            Connection con = JdbcUtil.getDbConnection(ClickHouseBulkDeleter.class);
            try (PreparedStatement stmt = con.prepareStatement(dropStatementString)) {
                stmt.executeUpdate();
            } catch (SQLException e) {
                if (! deletionHasBeenExecuted) {
                    // it is fatal to fail to DROP any old staging tables during the first call
                    // (during setup for deletion), because then the CREATE for staging will fail.
                    // on the second call, so long as the deletion completed, it is "ok" to leave behind
                    // residual staging tables -- they will (hopefully) DROP during the next update run
                    throw e;
                }
            } finally {
                JdbcUtil.closeAll(ClickHouseBulkDeleter.class, con, null, null);
            }
        }
    }

    // --- instance methods ---

    public void addId(long id) {
        pendingIds.add(id);
    }

    public void addIds(Collection<? extends Number> ids) {
        for (Number id : ids) {
            pendingIds.add(id.longValue());
        }
    }

    private ClickHouseBulkDeleter(String targetTable, String idColumn) {
        this.targetTable = targetTable;
        this.idColumn = idColumn;
        this.stagingTable = String.format("staging_delete_%s", targetTable);
    }

    private void populateStagingTable() throws SQLException, IOException {
        if (pendingIds.isEmpty()) {
            return;
        }
        // Insert IDs into staging table via TSV stream
        byte[] payload = buildTsvPayload();
        String insertStatementString = String.format(
                "INSERT INTO %s (id) FORMAT TSVWithNames",
                stagingTable);
        Connection con = JdbcUtil.getDbConnection(ClickHouseBulkDeleter.class);
        try (PreparedStatement stmt = con.prepareStatement(insertStatementString)) {
            stmt.setBinaryStream(1, new ByteArrayInputStream(payload));
            stmt.executeUpdate();
        } finally {
            JdbcUtil.closeAll(ClickHouseBulkDeleter.class, con, null, null);
        }
    }

    @FunctionalInterface
    private interface ConditionPredicate {
        boolean conditionIsSatisfied() throws SQLException, DaoException;
    }

    private boolean conditionIsTrueAfterTestWithRetry(ConditionPredicate conPred, int maxTestingSeconds) throws SQLException, DaoException {
        int WAIT_CYCLE_PERIOD_SECONDS = 10;
        int WAIT_CYCLE_MAX_COUNT = maxTestingSeconds / WAIT_CYCLE_PERIOD_SECONDS;
        int WAIT_CYCLE_TOLERATE_EXCEPTION_LIMIT = 6;
        int exceptionsEncountered = 0;
        boolean returnValue = false;
        for (int cycle = 0 ; cycle < WAIT_CYCLE_MAX_COUNT ; cycle = cycle + 1) {
            try {
                if (conPred.conditionIsSatisfied()) {
                    returnValue = true;
                    break;
                }
            } catch (SQLException e) {
                exceptionsEncountered = exceptionsEncountered + 1;
                if (exceptionsEncountered > WAIT_CYCLE_TOLERATE_EXCEPTION_LIMIT) {
                    throw e;
                }
            } finally {
                if (returnValue == false && cycle < WAIT_CYCLE_MAX_COUNT - 1 && exceptionsEncountered <= WAIT_CYCLE_TOLERATE_EXCEPTION_LIMIT) {
                    // sleep if we will be trying another iteration
                    try {
                        Thread.sleep(1000 * WAIT_CYCLE_PERIOD_SECONDS);
                    } catch (InterruptedException ie) {
                        // allow sleep interruption, and go on to next cycle immediately
                        Thread.currentThread().interrupt(); // set interrupted status, in case we want to adjust for interrupted sleep
                    }
                }
            }
        }
        return returnValue;
    }

    private Set<String> getReplicaHostnames() throws SQLException, DaoException {
        Set<String> hostnameSet = new HashSet<>();
        Connection con = JdbcUtil.getDbConnection(ClickHouseBulkDeleter.class);
        String getHostNamesQueryString = "SELECT hostname() as host from clusterAllReplicas('default', 'system', 'one') GROUP BY host";
        try (PreparedStatement pstmt = con.prepareStatement(getHostNamesQueryString);
                ResultSet rs = pstmt.executeQuery()) {
            while (rs.next()) {
                hostnameSet.add(rs.getString("host"));
            }
        } finally {
            JdbcUtil.closeAll(ClickHouseBulkDeleter.class, con, null, null);
        }
        if (hostnameSet.isEmpty()) {
            throw new DaoException(String.format(
                    "unable to find set of active replica hosts using query : %s",
                    getHostNamesQueryString));
        }
        return hostnameSet;
    }

    // queryString must be a SQL query which returns results which includes a field "host" with the hostname() of the replica responding. "SELECT hostname() as host..."
    private boolean allReplicasReportExpectedResult(String queryString, String outputField, long expectedResult) throws SQLException, DaoException {
        boolean allReplicasAreAsExpected = false;
        Set<String> allReplicaHostNames = getReplicaHostnames();
        Connection con = JdbcUtil.getDbConnection(ClickHouseBulkDeleter.class);
        try (PreparedStatement pstmt = con.prepareStatement(queryString);
                ResultSet rs = pstmt.executeQuery()) {
            boolean anUnexpectedResultWasSeen = false;
            Set<String> reportedReplicaHostNames = new HashSet<>();
            while (rs.next()) {
                String replicaHostname = rs.getString("host");
                reportedReplicaHostNames.add(replicaHostname);
                long resultOnReplica = rs.getLong(outputField);
                if (resultOnReplica != expectedResult) {
                    log.warn(String.format(
                            "waiting to reach expected result for query '%s' : expected %d saw %d on replica %s",
                            queryString,
                            expectedResult,
                            resultOnReplica,
                            replicaHostname));
                    anUnexpectedResultWasSeen = true;
                    break;
                }
            }
            if (!reportedReplicaHostNames.containsAll(allReplicaHostNames)) {
                // .containsAll() was used instead of .equals() because if additional replicas became active while the query was running,
                // that is still acceptable so long as the new replicas all report the expected result. we might worry about additional non-responding replicas...
                log.warn(String.format(
                        "while querying all replicas using query '%s' : either the replica set was reduced during the query execution or one or more replicas failed to be evaluated. waiting will continue. All replicas : %s ; Reported replicas : %s",
                        queryString,
                        String.join(", ", allReplicaHostNames),
                        String.join(", ", reportedReplicaHostNames)));
            } else {
                if (!anUnexpectedResultWasSeen) {
                    allReplicasAreAsExpected = true;
                }
            }
        } finally {
            JdbcUtil.closeAll(ClickHouseBulkDeleter.class, con, null, null);
        }
        return allReplicasAreAsExpected;
    }

    private boolean allReplicasReportStagingTableExists() throws SQLException, DaoException {
        String getTableExistsString = String.format(
                "SELECT hostname() as host, count() as table_exists FROM clusterAllReplicas('default', 'system', 'tables') where database = current_database() and name = '%s' GROUP BY host",
                stagingTable);
        return allReplicasReportExpectedResult(getTableExistsString, "table_exists", 1);
    }

    private boolean allReplicasReportExpectedStagingTableRecordCount() throws SQLException, DaoException {
        String getRecordCountsString = String.format(
                "SELECT hostname() as host, count() as record_count FROM clusterAllReplicas('default', current_database(), %s) GROUP BY host",
                stagingTable);
        return allReplicasReportExpectedResult(getRecordCountsString, "record_count", pendingIds.size());
    }

    private long deleteRecordsReferencedInStagingTable() throws SQLException {
        long records_deleted;
        String statementString = String.format(
                "DELETE FROM %s WHERE %s IN (SELECT id FROM %s)",
                targetTable, idColumn, stagingTable);
        Connection con = JdbcUtil.getDbConnection(ClickHouseBulkDeleter.class);
        try (PreparedStatement stmt = con.prepareStatement(statementString)) {
            records_deleted = stmt.executeUpdate();
        } finally {
            JdbcUtil.closeAll(ClickHouseBulkDeleter.class, con, null, null);
        }
        return records_deleted;
    }

    private boolean allReplicasCompletedDeletion() throws SQLException, DaoException {
        String getUndeletedRecordCountsString = String.format(
                "SELECT hostname() as host, count() as record_count FROM clusterAllReplicas('default', current_database(), %s) WHERE %s IN (SELECT id from %s) GROUP BY host",
                targetTable, idColumn, stagingTable);
        return allReplicasReportExpectedResult(getUndeletedRecordCountsString, "record_count", 0);
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
