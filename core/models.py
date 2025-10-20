# core/models.py
from django.conf import settings
from django.db import models, transaction as db_txn
from django.utils import timezone

User = settings.AUTH_USER_MODEL


class SiteSettings(models.Model):
    openai_api_key = models.CharField(max_length=255, blank=True, null=True)
    youtube_api_key = models.CharField(max_length=255, blank=True, null=True)
    rate_per_1000_views = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    min_withdraw_amount = models.DecimalField(max_digits=100, decimal_places=2, default=0)

    def __str__(self):
        return "Site Settings"

    @classmethod
    def load(cls):
        """
        Return the singleton SiteSettings row. Create it if doesn't exist.
        """
        obj = cls.objects.first()
        if obj is None:
            obj = cls.objects.create()
        return obj


class FileBatch(models.Model):
    file_name = models.CharField(max_length=200)
    seed_keyword = models.CharField(max_length=200)
    title_count = models.IntegerField(default=0)
    suggest_count = models.IntegerField(default=0)
    desc_length = models.IntegerField(default=0)
    suggestions = models.JSONField(default=list)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return self.file_name


class FileItem(models.Model):
    batch = models.ForeignKey(FileBatch, related_name="items", on_delete=models.CASCADE)
    title = models.TextField()
    description = models.TextField(blank=True)
    tags = models.TextField(blank=True)
    reuse_limit = models.IntegerField(default=2)
    used_count = models.IntegerField(default=0)
    is_used = models.BooleanField(default=False)


class Work(models.Model):
    name = models.CharField(max_length=200)
    file_batch = models.ForeignKey(FileBatch, related_name="works", on_delete=models.CASCADE)
    price_per_item = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deadline_minutes = models.PositiveIntegerField(default=60)
    video_zip = models.FileField(upload_to="videos/", blank=True, null=True)
    total_slots = models.PositiveIntegerField(default=0)
    remaining_slots = models.PositiveIntegerField(default=0)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.name} ({self.remaining_slots} left)"


class WorkClaim(models.Model):
    STATUS_CHOICES = [("claimed", "Claimed"), ("submitted", "Submitted"), ("expired", "Expired")]
    REVIEW_CHOICES = [("pending_review", "Pending Review"), ("approved", "Approved"), ("rejected", "Rejected")]

    user = models.ForeignKey(User, on_delete=models.CASCADE, related_name="claims")
    work = models.ForeignKey(Work, on_delete=models.CASCADE, related_name="claims")
    file_item = models.ForeignKey(FileItem, on_delete=models.CASCADE, null=True, blank=True)

    title = models.TextField(blank=True)
    description = models.TextField(blank=True)
    tags = models.TextField(blank=True)

    payout_amount = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="claimed")
    review_status = models.CharField(max_length=20, choices=REVIEW_CHOICES, default="pending_review")
    client_id = models.CharField(max_length=50, blank=True)

    assigned_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True)
    submitted_at = models.DateTimeField(null=True, blank=True)
    youtube_url = models.URLField(blank=True)
    youtube_video_id = models.CharField(max_length=50, blank=True)

    yt_views = models.PositiveBigIntegerField(default=0)
    yt_likes = models.PositiveBigIntegerField(default=0)
    yt_last_checked_at = models.DateTimeField(null=True, blank=True)
    next_check_at = models.DateTimeField(null=True, blank=True)
    views_paid_units = models.PositiveIntegerField(default=0)

    def __str__(self):
        return f"{self.user} - {self.work}"

    class Meta:
        constraints = [
            models.UniqueConstraint(fields=["user", "work"], name="uniq_user_work")
        ]


class ClaimMetricsLog(models.Model):
    claim = models.ForeignKey(WorkClaim, related_name="metrics_log", on_delete=models.CASCADE)
    snapshot_at = models.DateTimeField(default=timezone.now)
    views = models.PositiveBigIntegerField(default=0)
    likes = models.PositiveBigIntegerField(default=0)

    class Meta:
        ordering = ["-snapshot_at"]

    def __str__(self):
        return f"Log for {self.claim_id} ({self.views} views)"


# ===================== WALLET =====================

class Wallet(models.Model):
    user = models.OneToOneField(User, related_name="wallet", on_delete=models.CASCADE)
    balance = models.DecimalField(max_digits=14, decimal_places=2, default=0)  # cached balance

    def __str__(self):
        return f"Wallet({self.user_id}) = {self.balance}"

    @classmethod
    def get_or_create_for_user(cls, user):
        obj, _ = cls.objects.get_or_create(user=user, defaults={"balance": 0})
        return obj


class WalletTransaction(models.Model):
    KIND_CHOICES = [
        ("task_credit", "Task Credit"),
        ("milestone_bonus", "Milestone Bonus"),
        ("withdrawal_hold", "Withdrawal Hold"),
        ("withdrawal", "Withdrawal Payout"),
        ("admin_adjustment", "Admin Adjustment"),
        ("reversal", "Reversal"),
    ]
    wallet = models.ForeignKey(Wallet, related_name="transactions", on_delete=models.CASCADE)
    kind = models.CharField(max_length=32, choices=KIND_CHOICES)
    amount = models.DecimalField(max_digits=14, decimal_places=2)  # signed (+ credit, - debit)
    ref_claim = models.ForeignKey(WorkClaim, null=True, blank=True, on_delete=models.SET_NULL)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["-created_at"]

    def __str__(self):
        sign = "+" if self.amount >= 0 else "-"
        return f"{self.wallet_id} {self.kind} {sign}{abs(self.amount)}"

    @staticmethod
    def apply_transaction(wallet: "Wallet", kind: str, amount, ref_claim=None, note=""):
        """
        Atomically apply a signed transaction and update wallet balance.
        """
        from decimal import Decimal
        with db_txn.atomic():
            w = Wallet.objects.select_for_update().get(pk=wallet.pk)
            txn = WalletTransaction.objects.create(
                wallet=w, kind=kind, amount=Decimal(amount), ref_claim=ref_claim, note=note
            )
            w.balance = w.balance + txn.amount
            w.save(update_fields=["balance"])
            return txn


class WithdrawalRequest(models.Model):
    STATUS_CHOICES = [("pending", "Pending"), ("approved", "Approved"), ("rejected", "Rejected")]

    wallet = models.ForeignKey(Wallet, related_name="withdrawals", on_delete=models.CASCADE)
    amount = models.DecimalField(max_digits=14, decimal_places=2)
    upi_vpa = models.CharField(max_length=100)  # e.g., name@bank
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="pending")
    requested_at = models.DateTimeField(auto_now_add=True)
    processed_at = models.DateTimeField(null=True, blank=True)
    admin_note = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"Withdraw#{self.pk} {self.status} {self.amount}"

    @property
    def is_pending(self):
        return self.status == "pending"


class MilestoneRule(models.Model):
    """
    Admin-defined payout rules per threshold views.
    """
    active = models.BooleanField(default=True)
    threshold_views = models.PositiveBigIntegerField(unique=True)
    payout_amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["threshold_views"]

    def __str__(self):
        return f"{self.threshold_views} views -> {self.payout_amount}"


class MilestonePayout(models.Model):
    """
    A specific claim hitting a specific rule, pending admin review,
    and credited exactly once when approved.
    """
    STATUS_CHOICES = [
        ("pending_review", "Pending Review"),
        ("approved", "Approved"),
        ("rejected", "Rejected"),
    ]

    claim = models.ForeignKey(WorkClaim, related_name="milestone_payouts", on_delete=models.CASCADE)
    rule = models.ForeignKey(MilestoneRule, related_name="claim_hits", on_delete=models.CASCADE)

    views_snapshot = models.PositiveBigIntegerField(default=0)
    likes_snapshot = models.PositiveBigIntegerField(default=0)
    amount = models.DecimalField(max_digits=14, decimal_places=2, default=0)

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="pending_review")
    created_at = models.DateTimeField(auto_now_add=True)
    decided_at = models.DateTimeField(null=True, blank=True)

    credited_txn = models.ForeignKey(WalletTransaction, null=True, blank=True, on_delete=models.SET_NULL)

    class Meta:
        ordering = ["-created_at"]
        constraints = [
            models.UniqueConstraint(fields=["claim", "rule"], name="uniq_claim_rule_once"),
        ]

    def __str__(self):
        return f"Milestone(claim={self.claim_id}, {self.rule.threshold_views}, {self.status})"
