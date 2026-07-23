package com.oncoord.auctionscout.saved;

import com.oncoord.auctionscout.properties.PropertyDigestRepository;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.time.OffsetDateTime;
import java.util.List;

/**
 * Reads/writes saved_properties in auctionscout-manager.db (the Java
 * service's own state, NOT the pipeline-owned auctionscout.db).
 *
 * Rows are a denormalized snapshot of the property at the time it was
 * saved (address/state/county/municipality/lat/lon), not a live join
 * to properties -- properties rows get purged ~60 days after a
 * property's auction/sale, and a saved property should keep showing
 * correctly after that happens.
 */
@Repository
public class SavedPropertiesRepository {

    public record SavedProperty(
            long propertyId, String addressRaw, String state, String county,
            String municipality, Double latitude, Double longitude, OffsetDateTime savedAt) {}

    private final JdbcTemplate jdbc;

    public SavedPropertiesRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    /** Idempotent: saving an already-saved property is a no-op, not an error. */
    public void save(String email, PropertyDigestRepository.PropertyDetails property) {
        jdbc.update("""
                INSERT INTO saved_properties
                    (email, property_id, address_raw, state, county, municipality, latitude, longitude, saved_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(email, property_id) DO NOTHING
                """,
                email, property.propertyId(), property.address(), property.state(),
                property.county(), property.municipality(), property.latitude(), property.longitude(),
                OffsetDateTime.now().toString());
    }

    /** Idempotent: unsaving something not saved is a no-op, not an error. */
    public void delete(String email, long propertyId) {
        jdbc.update("DELETE FROM saved_properties WHERE email = ? AND property_id = ?", email, propertyId);
    }

    public List<SavedProperty> findByEmail(String email) {
        return jdbc.query("""
                SELECT property_id, address_raw, state, county, municipality, latitude, longitude, saved_at
                FROM saved_properties
                WHERE email = ?
                ORDER BY saved_at DESC
                """,
                (rs, rowNum) -> new SavedProperty(
                        rs.getLong("property_id"),
                        rs.getString("address_raw"),
                        rs.getString("state"),
                        rs.getString("county"),
                        rs.getString("municipality"),
                        (Double) rs.getObject("latitude"),
                        (Double) rs.getObject("longitude"),
                        OffsetDateTime.parse(rs.getString("saved_at"))),
                email);
    }
}