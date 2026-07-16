package com.oncoord.auctionscout.mail;

import org.springframework.beans.factory.annotation.Value;
import org.springframework.mail.javamail.JavaMailSender;
import org.springframework.mail.javamail.MimeMessageHelper;
import org.springframework.stereotype.Component;

import jakarta.mail.MessagingException;
import jakarta.mail.internet.MimeMessage;

/**
 * Sends digest emails (welcome / weekly / test) — the mailer
 * OneTimeLinkMailer's class javadoc anticipated ("digest mailer, built
 * later"). Shares the same JavaMailSender bean and auctionscout.mail.from
 * config as OneTimeLinkMailer, but keeps its own small send() rather
 * than extracting a shared helper, so the working registration-email
 * path stays untouched.
 */
@Component
public class DigestMailer {

    private final JavaMailSender mailSender;
    private final String fromAddress;

    public DigestMailer(JavaMailSender mailSender,
                        @Value("${auctionscout.mail.from}") String fromAddress) {
        this.mailSender = mailSender;
        this.fromAddress = fromAddress;
    }

    public void send(String to, String subject, String html) {
        MimeMessage message = mailSender.createMimeMessage();
        try {
            MimeMessageHelper helper = new MimeMessageHelper(message, false, "UTF-8");
            helper.setFrom(fromAddress);
            helper.setTo(to);
            helper.setSubject(subject);
            helper.setText(html, true);
        } catch (MessagingException e) {
            // See OneTimeLinkMailer for the same reasoning — this
            // checked exception is essentially unreachable in practice.
            throw new IllegalStateException("Failed to build email message", e);
        }
        mailSender.send(message);
    }
}