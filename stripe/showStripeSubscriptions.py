"""
stripe_subscription_manager.py

Lists customers with subscriptions, and optionally cancels all active
subscriptions immediately (e.g. if you're shutting down a business and
need to make sure no customer gets charged again).

Setup:
    pip install stripe

    export AUCTIONSCOUT_STRIPE_SECRET_KEY=sk_test_...   # use sk_test_ first, sk_live_ when ready

Usage:
    python stripe_subscription_manager.py list         # list subscriptions only
    python stripe_subscription_manager.py history       # full paid-invoice history per customer
    python stripe_subscription_manager.py cancel        # list, then prompt to cancel all
    python stripe_subscription_manager.py cancel --yes  # skip the confirmation prompt
"""

import os
import sys
import csv
from datetime import datetime, timezone
import stripe


def get_api_key() -> str:
    """Load the Stripe API key from the environment, or exit with a clear error."""
    api_key = os.environ.get("AUCTIONSCOUT_STRIPE_SECRET_KEY")
    if not api_key:
        sys.exit(
            "ERROR: AUCTIONSCOUT_STRIPE_SECRET_KEY environment variable is not set.\n"
            "Set it before running this script, e.g.:\n"
            "    export AUCTIONSCOUT_STRIPE_SECRET_KEY=sk_test_...\n"
        )
    return api_key


def format_amount(amount_cents, currency):
    """Convert Stripe's integer cents amount into a human-readable string."""
    if amount_cents is None:
        return ""
    return f"{amount_cents / 100:.2f} {currency.upper()}"


def get_last_paid_invoice(customer_id):
    """Find the most recent successfully PAID invoice for a customer.
    (latest_invoice on a subscription can be an unbilled draft for the
    upcoming period, so we query paid invoices directly instead.)"""
    invoices = stripe.Invoice.list(customer=customer_id, status="paid", limit=1)
    if invoices.data:
        return invoices.data[0]
    return None


def get_upcoming_invoice(customer_id, subscription_id=None):
    """Preview the next invoice Stripe will generate for this subscription.
    Returns None if there's no upcoming invoice (e.g. subscription is
    canceled, or on a plan with nothing left to bill).

    Stripe deprecated `Invoice.upcoming` in favor of `Invoice.create_preview`
    (2025-03-31 API version). We try the new method first and fall back to
    the old one for anyone on an older stripe-python version.
    """
    try:
        if hasattr(stripe.Invoice, "create_preview"):
            return stripe.Invoice.create_preview(
                customer=customer_id, subscription=subscription_id
            )
        return stripe.Invoice.upcoming(
            customer=customer_id, subscription=subscription_id
        )
    except stripe.error.InvalidRequestError:
        return None



def list_subscriptions(status: str = "all"):
    """Return a list of dicts: one row per subscription, with customer info,
    last actual paid invoice, and diagnostics that explain why an 'active'
    subscription might not actually be getting charged."""
    rows = []
    subscriptions = stripe.Subscription.list(
        status=status,
        limit=100,
        expand=["data.customer", "data.latest_invoice"],
    )
    for sub in subscriptions.auto_paging_iter():
        customer = sub.customer
        invoice = get_last_paid_invoice(customer.id)

        last_payment_date = ""
        last_payment_amount = ""
        last_invoice_status = ""

        if invoice:
            last_invoice_status = invoice.status or ""
            last_payment_amount = format_amount(invoice.amount_paid, invoice.currency)
            paid_at = None
            if invoice.status_transitions and invoice.status_transitions.paid_at:
                paid_at = invoice.status_transitions.paid_at
            elif invoice.created:
                paid_at = invoice.created
            if paid_at:
                last_payment_date = datetime.fromtimestamp(
                    paid_at, tz=timezone.utc
                ).strftime("%Y-%m-%d")
        else:
            last_invoice_status = "no paid invoices"

        # Diagnostics: why might an "active" subscription not be getting charged?
        collection_method = sub.collection_method or ""
        is_paused = bool(sub.pause_collection)
        current_period_end = (
            datetime.fromtimestamp(sub.current_period_end, tz=timezone.utc).strftime("%Y-%m-%d")
            if getattr(sub, "current_period_end", None)
            else ""
        )
        # latest_invoice regardless of paid/unpaid — reveals stuck/open invoices
        latest_inv = sub.latest_invoice
        latest_invoice_status_any = latest_inv.status if latest_inv else ""

        # Next scheduled payment — use Stripe's upcoming-invoice preview,
        # which reflects proration/discounts, rather than guessing from
        # current_period_end alone.
        next_payment_date = ""
        next_payment_amount = ""
        upcoming = get_upcoming_invoice(customer.id, subscription_id=sub.id)
        if upcoming:
            next_payment_amount = format_amount(upcoming.amount_due, upcoming.currency)
            # next_payment_attempt is set once the invoice is finalized and
            # queued for a charge attempt; before that, current_period_end
            # is the best estimate of when it will be generated/attempted.
            if getattr(upcoming, "next_payment_attempt", None):
                next_payment_date = datetime.fromtimestamp(
                    upcoming.next_payment_attempt, tz=timezone.utc
                ).strftime("%Y-%m-%d")
            else:
                next_payment_date = current_period_end
        elif collection_method == "send_invoice":
            next_payment_date = "N/A (manual invoicing)"

        rows.append(
            {
                "customer_id": customer.id,
                "customer_email": customer.email or "",
                "customer_name": customer.name or "",
                "subscription_id": sub.id,
                "status": sub.status,
                "last_payment_date": last_payment_date,
                "last_payment_amount": last_payment_amount,
                "last_invoice_status": last_invoice_status,
                "collection_method": collection_method,
                "paused": is_paused,
                "current_period_end": current_period_end,
                "latest_invoice_status": latest_invoice_status_any,
                "next_payment_date": next_payment_date,
                "next_payment_amount": next_payment_amount,
            }
        )
    return rows


def get_payment_history(customer_id):
    """Return every successfully PAID invoice for a customer, most recent first."""
    history = []
    invoices = stripe.Invoice.list(customer=customer_id, status="paid", limit=100)
    for inv in invoices.auto_paging_iter():
        paid_at = None
        if inv.status_transitions and inv.status_transitions.paid_at:
            paid_at = inv.status_transitions.paid_at
        elif inv.created:
            paid_at = inv.created
        history.append(
            {
                "date": datetime.fromtimestamp(paid_at, tz=timezone.utc).strftime("%Y-%m-%d")
                if paid_at
                else "",
                "amount": format_amount(inv.amount_paid, inv.currency),
                "invoice_id": inv.id,
                "invoice_number": inv.number or "",
            }
        )
    return history


def list_all_payment_history(status: str = "all"):
    """Return one row PER PAYMENT (not per subscription/customer) across all
    customers who have a subscription. Use for a full payment history dump."""
    rows = []
    subscriptions = stripe.Subscription.list(
        status=status,
        limit=100,
        expand=["data.customer"],
    )
    seen_customers = set()
    for sub in subscriptions.auto_paging_iter():
        customer = sub.customer
        if customer.id in seen_customers:
            continue  # avoid duplicate history if a customer has 2+ subscriptions
        seen_customers.add(customer.id)

        payments = get_payment_history(customer.id)
        if not payments:
            rows.append(
                {
                    "customer_id": customer.id,
                    "customer_email": customer.email or "",
                    "payment_date": "",
                    "amount": "",
                    "invoice_id": "",
                    "invoice_number": "",
                }
            )
            continue

        for p in payments:
            rows.append(
                {
                    "customer_id": customer.id,
                    "customer_email": customer.email or "",
                    "payment_date": p["date"],
                    "amount": p["amount"],
                    "invoice_id": p["invoice_id"],
                    "invoice_number": p["invoice_number"],
                }
            )
    return rows


def print_history_table(rows):
    if not rows:
        print("No payment history found.")
        return
    print(f"{'CUSTOMER ID':<22} {'EMAIL':<28} {'DATE':<12} {'AMOUNT':<15} INVOICE #")
    for r in rows:
        print(
            f"{r['customer_id']:<22} {r['customer_email']:<28} "
            f"{r['payment_date']:<12} {r['amount']:<15} {r['invoice_number']}"
        )
    print(f"\nTotal: {len(rows)} payment record(s)")


def print_table(rows):
    if not rows:
        print("No subscriptions found.")
        return
    print(
        f"{'CUSTOMER ID':<22} {'EMAIL':<26} {'STATUS':<10} {'LAST PAID':<11} "
        f"{'AMOUNT':<9} {'NEXT DUE':<20} {'NEXT AMT':<10} {'COLLECTION':<16} {'PAUSED'}"
    )
    for r in rows:
        print(
            f"{r['customer_id']:<22} {r['customer_email']:<26} {r['status']:<10} "
            f"{r['last_payment_date']:<11} {r['last_payment_amount']:<9} "
            f"{r['next_payment_date']:<20} {r['next_payment_amount']:<10} "
            f"{r['collection_method']:<16} {str(r['paused'])}"
        )
    print(f"\nTotal: {len(rows)} subscription(s)")


def save_csv(rows, filename="subscriptions.csv"):
    if not rows:
        return
    with open(filename, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=rows[0].keys())
        writer.writeheader()
        writer.writerows(rows)
    print(f"Saved details to {filename}")


def cancel_all_active(rows):
    """Cancel every subscription in `rows` immediately. Returns (succeeded, failed)."""
    active_rows = [r for r in rows if r["status"] not in ("canceled", "incomplete_expired")]
    succeeded, failed = [], []
    for r in active_rows:
        try:
            stripe.Subscription.cancel(r["subscription_id"])
            succeeded.append(r)
            print(f"Canceled {r['subscription_id']} ({r['customer_email']})")
        except stripe.error.StripeError as e:
            failed.append(r)
            print(f"FAILED to cancel {r['subscription_id']}: {e.user_message or e}")
    return succeeded, failed


def main():
    stripe.api_key = get_api_key()

    if len(sys.argv) < 2 or sys.argv[1] not in ("list", "cancel", "history"):
        sys.exit("Usage: python stripe_subscription_manager.py [list|cancel|history] [--yes]")

    command = sys.argv[1]
    skip_confirm = "--yes" in sys.argv

    if command == "history":
        print("Fetching full payment history from Stripe...\n")
        rows = list_all_payment_history(status="all")
        print_history_table(rows)
        save_csv(rows, filename="payment_history.csv")
        return

    print("Fetching subscriptions from Stripe...\n")
    rows = list_subscriptions(status="all")
    print_table(rows)
    save_csv(rows)

    if command == "cancel":
        active_count = len([r for r in rows if r["status"] not in ("canceled", "incomplete_expired")])
        if active_count == 0:
            print("\nNo active subscriptions to cancel.")
            return

        if not skip_confirm:
            confirm = input(
                f"\nThis will IMMEDIATELY cancel {active_count} subscription(s) "
                f"and stop all future charges. Type 'yes' to proceed: "
            )
            if confirm.strip().lower() != "yes":
                print("Aborted. No subscriptions were canceled.")
                return

        print()
        succeeded, failed = cancel_all_active(rows)
        print(f"\nDone. Canceled: {len(succeeded)}. Failed: {len(failed)}.")
        if failed:
            print("Review failed cancellations manually in the Stripe Dashboard.")


if __name__ == "__main__":
    main()