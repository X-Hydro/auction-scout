package com.oncoord.auctionscout.service;

import org.slf4j.Logger;
import org.slf4j.LoggerFactory;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.scheduling.annotation.Scheduled;
import org.springframework.stereotype.Service;

import java.io.IOException;
import java.nio.file.*;
import java.time.*;
import java.time.format.DateTimeFormatter;

/**
 * Nightly backup of the AuctionScout manager SQLite DB (subscriber/state
 * data), mirroring oncoord-manager's DatabaseBackupService. Unlike the
 * OnCoord version, there's no "recent activity" signal to gate on here —
 * subscriber volume is low and steady, so this backs up unconditionally
 * every night rather than skipping quiet days.
 */
@Service
public class DatabaseBackupService {

    @Value("${auctionscout.db.path:auctionscout-manage.db}")
    private String dbPath;

    private static final String BACKUP_DIR = "backups";
    private static final int RETENTION_DAYS = 10;
    private static final DateTimeFormatter TS_FORMAT = DateTimeFormatter.ofPattern("yyyyMMdd_HHmmss");

    private static final Logger logger = LoggerFactory.getLogger(DatabaseBackupService.class);

    /**
     * Perform a single daily backup. Runs at midnight.
     */
    @Scheduled(cron = "0 0 0 * * *") // once per day at midnight
    public void dailyBackup() {
        performBackup("daily");
    }

    /**
     * Perform a single backup copy with the given label (e.g., "daily").
     */
    private void performBackup(String label) {
        Path dbFile = Paths.get(dbPath);

        try {
            if (!Files.exists(dbFile)) {
                logger.error("[Backup] ❌ auctionscout-manage.db not found at {}", dbPath);
                return;
            }

            Path backupDir = dbFile.getParent().resolve(BACKUP_DIR);
            if (!Files.exists(backupDir)) {
                Files.createDirectories(backupDir);
            }

            String timestamp = LocalDateTime.now().format(TS_FORMAT);
            Path backupFile = backupDir.resolve("auctionscout-manage_" + label + "_" + timestamp + ".db");

            Files.copy(dbFile, backupFile, StandardCopyOption.REPLACE_EXISTING);
            logger.info("[Backup] ✅ Created {} backup: {}", label, backupFile.getFileName());
        } catch (IOException e) {
            logger.error("[Backup] ⚠️ Failed to create {} backup at {}: {}", label, dbPath, e.getMessage(), e);
        } catch (Exception e) {
            logger.error("[Backup] ⚠️ Unexpected error during {} backup: {}", label, e.getMessage(), e);
        }
    }

    /**
     * Prune backups older than the retention period (14 days).
     * Runs daily at 00:15.
     */
    @Scheduled(cron = "0 15 0 * * *")
    public void pruneOldBackups() throws IOException {
        Path dbFile = Paths.get(dbPath);
        Path backupDir = dbFile.getParent().resolve(BACKUP_DIR);

        if (!Files.exists(backupDir)) return;

        Instant cutoff = Instant.now().minus(Duration.ofDays(RETENTION_DAYS));

        try (DirectoryStream<Path> stream = Files.newDirectoryStream(backupDir, "*.db")) {
            for (Path file : stream) {
                if (Files.getLastModifiedTime(file).toInstant().isBefore(cutoff)) {
                    Files.delete(file);
                    logger.info("[Backup] Deleted old backup: {}", file.getFileName());
                }
            }
        }
    }
}