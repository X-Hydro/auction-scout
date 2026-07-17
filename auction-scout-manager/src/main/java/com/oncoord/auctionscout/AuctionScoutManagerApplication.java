package com.oncoord.auctionscout;

import org.springframework.boot.SpringApplication;
import org.springframework.boot.autoconfigure.SpringBootApplication;
import org.springframework.scheduling.annotation.EnableScheduling;

@SpringBootApplication
@EnableScheduling
public class AuctionScoutManagerApplication {

    public static void main(String[] args) {
        SpringApplication.run(AuctionScoutManagerApplication.class, args);
    }
}