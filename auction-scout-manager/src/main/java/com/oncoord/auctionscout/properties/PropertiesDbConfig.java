package com.oncoord.auctionscout.properties;

import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;
import org.springframework.jdbc.datasource.SingleConnectionDataSource;

import javax.sql.DataSource;

/**
 * A second, separate datasource pointed at auctionscout.db — the
 * Python-written properties/auctions/auction_events database. Kept
 * completely distinct from the primary DataSource (auctionscout-manage.db,
 * autoconfigured from spring.datasource.* for subscribers/login_tokens)
 * since these are two different SQLite files. Neither @Primary nor
 * unqualified injection would work here without ambiguity — every
 * consumer must use @Qualifier("propertiesJdbcTemplate") explicitly.
 */
@Configuration
public class PropertiesDbConfig {

    @Bean(name = "propertiesDataSource")
    public DataSource propertiesDataSource(
            @Value("${auctionscout.properties-db.path}") String dbPath) {
        // autoCommit=true, no schema init — this service only reads from
        // this file, and the Python pipeline owns its schema entirely.
        return new SingleConnectionDataSource("jdbc:sqlite:" + dbPath, true);
    }

    @Bean(name = "propertiesJdbcTemplate")
    public JdbcTemplate propertiesJdbcTemplate(
            @Qualifier("propertiesDataSource") DataSource propertiesDataSource) {
        return new JdbcTemplate(propertiesDataSource);
    }
}