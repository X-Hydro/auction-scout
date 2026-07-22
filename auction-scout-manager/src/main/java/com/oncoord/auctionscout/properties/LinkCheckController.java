package com.oncoord.auctionscout.properties;

import org.springframework.web.bind.annotation.GetMapping;
import org.springframework.web.bind.annotation.PathVariable;
import org.springframework.web.bind.annotation.RestController;

import java.util.Map;

@RestController
public class LinkCheckController {

    private final LinkCheckService linkCheckService;

    public LinkCheckController(LinkCheckService linkCheckService) {
        this.linkCheckService = linkCheckService;
    }

    @GetMapping("/link-check/{propertyId}")
    public Map<String, Boolean> checkLink(@PathVariable long propertyId) {
        LinkCheckService.LinkCheckResult result = linkCheckService.check(propertyId);
        // "found" isn't surfaced separately -- an unknown property_id and a
        // confirmed-live link both just mean "let the click through";
        // the frontend only branches on alive.
        return Map.of("alive", result.alive());
    }
}