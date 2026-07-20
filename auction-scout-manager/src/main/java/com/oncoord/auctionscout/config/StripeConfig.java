package com.oncoord.auctionscout.config;

import com.stripe.Stripe;
import org.springframework.beans.factory.annotation.Value;
import org.springframework.context.annotation.Configuration;

/**
 * Sets the Stripe SDK's static Stripe.apiKey once at startup. The
 * Stripe Java SDK authenticates via this single static field rather
 * than a per-call parameter -- every real Stripe API call anywhere in
 * the app (Session.create() in SubscriptionController.checkout(),
 * Subscription.retrieve(...).cancel() in SubscriptionController.
 * cancel()) relies on this already being set before that call happens.
 * Without this class, both of those fail with a Stripe authentication
 * error, since Stripe.apiKey defaults to null.
 *
 * A plain @Configuration constructor rather than @PostConstruct: Spring
 * instantiates singleton beans -- including this one -- during context
 * refresh, which always completes before Tomcat starts accepting HTTP
 * requests, so this is guaranteed to have run before any controller
 * method that needs it.
 */
@Configuration
public class StripeConfig {

    public StripeConfig(@Value("${auctionscout.stripe.secret-key}") String secretKey) {
        Stripe.apiKey = secretKey;
    }
}