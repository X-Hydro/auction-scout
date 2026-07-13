package com.oncoord.auctionscout.subscriber;

import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.stereotype.Repository;

import java.util.Optional;

/**
 * Minimal subscriber persistence — deliberately small right now so
 * register/verify can compile end-to-end. Extend with subscriber_states
 * (max 4) once the states UI is wired up.
 */
@Repository
public class SubscriberRepository {

    private final JdbcTemplate jdbc;

    public SubscriberRepository(JdbcTemplate jdbc) {
        this.jdbc = jdbc;
    }

    public boolean existsByEmail(String email) {
        Integer count = jdbc.queryForObject(
                "SELECT COUNT(*) FROM subscribers WHERE email = ?",
                Integer.class, email
        );
        return count != null && count > 0;
    }

    public void createUnverified(String email) {
        jdbc.update(
                "INSERT INTO subscribers (email, created_at, verified_at, is_active) " +
                        "VALUES (?, ?, NULL, 0)",
                email, System.currentTimeMillis()
        );
    }

    public void markVerified(String email) {
        jdbc.update(
                "UPDATE subscribers SET verified_at = ?, is_active = 1 WHERE email = ?",
                System.currentTimeMillis(), email
        );
    }

    public Optional<String> findActiveEmail(String email) {
        return jdbc.query(
                "SELECT email FROM subscribers WHERE email = ? AND is_active = 1",
                rs -> rs.next() ? Optional.of(rs.getString("email")) : Optional.empty(),
                email
        );
    }
}