package com.oncoord.auctionscout.config;

import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.web.servlet.config.annotation.CorsRegistry;
import org.springframework.web.servlet.config.annotation.WebMvcConfigurer;

/**
 * Without this, every fetch() from register.html/post-login.html/
 * preferences.html is blocked by the browser — not a backend bug, a
 * browser security rule that applies whenever JS on one origin calls a
 * server on a different origin, unless that server explicitly allows it.
 * Frontend (oncoord-frontend, Static Web Apps) and this API run on
 * different hosts, so this is required, not optional.
 */
@Configuration
public class CorsConfig implements WebMvcConfigurer {

    @Override
    public void addCorsMappings(CorsRegistry registry) {
        registry.addMapping("/**")
                .allowedOrigins(
                        "http://localhost:8080",       // local static server, per Thale's setup
                        "https://www.oncoord.com"       // production
                )
                .allowedMethods("GET", "POST")
                // X-Session-Token is a custom header, so browsers preflight
                // it with an OPTIONS request first — without this explicit
                // allow, the preflight itself gets rejected and the real
                // request never goes out at all.
                .allowedHeaders("Content-Type", "X-Session-Token")
                .allowCredentials(false); // no cookies in play — bearer token only
    }
}