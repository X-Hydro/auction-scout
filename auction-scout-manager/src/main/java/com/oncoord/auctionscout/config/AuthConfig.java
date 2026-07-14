package com.oncoord.auctionscout.config;

import com.oncoord.auctionscout.auth.AuctionScoutTokenStore;
import com.oncoord.auth.common.RecaptchaClient;
import com.oncoord.auth.common.TokenService;
import com.oncoord.auth.common.TokenStore;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jdbc.core.JdbcTemplate;

@Configuration
public class AuthConfig {

    @Bean
    public TokenStore tokenStore(JdbcTemplate jdbcTemplate) {
        return new AuctionScoutTokenStore(jdbcTemplate);
    }

    @Bean
    public TokenService tokenService(TokenStore tokenStore) {
        return new TokenService(tokenStore);
    }

    @Bean
    public RecaptchaClient recaptchaClient(
            @Value("${recaptcha.secret-key}") String secret) {
        return new RecaptchaClient(secret);
    }
}