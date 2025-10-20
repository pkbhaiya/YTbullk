# core/utils/payout.py
from decimal import Decimal
from django.db import transaction
from django.utils import timezone

from core.models import Wallet, WalletTransaction, WorkClaim


def credit_claim_if_not_credited(claim: WorkClaim, note: str = ""):
    """
    Idempotent: credit user's wallet for the given claim only once.
    Returns the created WalletTransaction instance, or None if already credited / nothing to do.
    Uses claim.payout_amount if set, else falls back to work.price_per_item.
    """
    if claim is None:
        raise ValueError("claim is required")

    # Determine amount
    try:
        amount = Decimal(claim.payout_amount or 0)
    except Exception:
        amount = Decimal("0")

    if amount <= 0:
        # fallback to work price_per_item
        wp = getattr(claim, "work", None)
        if wp:
            try:
                amount = Decimal(getattr(wp, "price_per_item", 0) or 0)
            except Exception:
                amount = Decimal("0")

    if amount <= 0:
        # Nothing meaningful to credit
        return None

    # If we already have a task_credit for this claim, don't double-credit
    existing = WalletTransaction.objects.filter(ref_claim=claim, kind="task_credit").first()
    if existing:
        return None

    # Ensure wallet exists
    wallet = Wallet.get_or_create_for_user(claim.user)

    # Use WalletTransaction.apply_transaction which is atomic
    note = note or f"Task payout for claim #{claim.id}"
    with transaction.atomic():
        txn = WalletTransaction.apply_transaction(wallet, "task_credit", amount, ref_claim=claim, note=note)
    return txn
