package com.oncoord.auctionscout.mail;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.mail.javamail.JavaMailSender;
import org.springframework.mail.javamail.MimeMessageHelper;
import org.springframework.stereotype.Component;

import jakarta.mail.MessagingException;
import jakarta.mail.internet.MimeMessage;
import java.net.URLEncoder;
import java.nio.charset.StandardCharsets;

/**
 * Sends the one-time magic link a subscriber clicks to confirm
 * registration (or log back in — same link, single implicit purpose,
 * per the login_tokens design). Kept separate from RegisterController
 * so the digest mailer (built later) can share the same JavaMailSender
 * config without duplicating SMTP wiring.
 */
@Component
public class OneTimeLinkMailer {

    private final JavaMailSender mailSender;
    private final String fromAddress;
    private final String appBaseUrl;

    public OneTimeLinkMailer(JavaMailSender mailSender,
                             @Value("${auctionscout.mail.from}") String fromAddress,
                             @Value("${auctionscout.app.base-url}") String appBaseUrl) {
        this.mailSender = mailSender;
        this.fromAddress = fromAddress;
        this.appBaseUrl = appBaseUrl;
    }

    public void sendRegistrationLink(String email, String rawToken) {
        // Points at the frontend page, not directly at the backend
        // /verify endpoint — matches the pattern the dev-only stdout
        // stub already used. post-login.html is responsible for calling
        // /verify itself (via fetch) and showing a real confirmation UI,
        // rather than the email link landing on bare JSON.
        String verifyUrl = appBaseUrl + "/auction-scout/post-login.html#email="
                + URLEncoder.encode(email, StandardCharsets.UTF_8)
                + "&token=" + URLEncoder.encode(rawToken, StandardCharsets.UTF_8);

        String subject = "Confirm your AuctionScout subscription";
        String html = """
                <div style="font-family:-apple-system,Helvetica,Arial,sans-serif;color:#1a1a1a;max-width:480px;margin:0 auto;">
                  <h2 style="color:#1a3a5c;">Confirm your email</h2>
                  <p>Click below to confirm your AuctionScout subscription and start getting
                  alerts for new and changed foreclosure auctions.</p>
                  <p style="margin:24px 0;">
                    <a href="%s" style="background:#1a3a5c;color:#ffffff;padding:10px 20px;
                       border-radius:4px;text-decoration:none;display:inline-block;">
                       Confirm subscription
                    </a>
                  </p>
                  <p style="font-size:13px;color:#666;">This link expires in 30 minutes.
                  If you didn't request this, you can ignore this email.</p>
                </div>
                """.formatted(verifyUrl);

        send(email, subject, html);
    }

    private void send(String to, String subject, String html) {
        MimeMessage message = mailSender.createMimeMessage();
        try {
            MimeMessageHelper helper = new MimeMessageHelper(message, false, "UTF-8");
            helper.setFrom(fromAddress);
            helper.setTo(to);
            helper.setSubject(subject);
            helper.setText(html, true);
        } catch (MessagingException e) {
            // MimeMessageHelper's checked exception is essentially
            // unreachable here (bad charset name, mainly) — wrap rather
            // than force every caller to declare a checked exception for
            // a failure mode that isn't actually reachable in practice.
            throw new IllegalStateException("Failed to build email message", e);
        }
        mailSender.send(message);
    }
}