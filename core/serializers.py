# core/serializers.py
import re
from django.contrib.auth import get_user_model
from rest_framework import serializers

from .models import (
    SiteSettings,
    FileBatch,
    FileItem,
    Work,
    WorkClaim,
    Wallet,
    WalletTransaction,
    WithdrawalRequest,
    MilestoneRule,
    MilestonePayout,
)

User = get_user_model()

# ----------------------------
# Site & Files
# ----------------------------
class SettingsSerializer(serializers.ModelSerializer):
    class Meta:
        model = SiteSettings
        fields = ["openai_api_key", "youtube_api_key", "rate_per_1000_views", "min_withdraw_amount"]


class FileItemSerializer(serializers.ModelSerializer):
    video_url = serializers.SerializerMethodField()

    class Meta:
        model = FileItem
        fields = ["id", "title", "reuse_limit", "used_count", "video_url"]

    def get_video_url(self, obj):
        request = self.context.get("request")

        # Prefer FileField named "file" if present
        if hasattr(obj, "file") and getattr(obj.file, "url", None):
            url = obj.file.url
        # Fallback: dedicated URL field on the model
        elif hasattr(obj, "video_url") and obj.video_url:
            url = obj.video_url
        else:
            return None

        return request.build_absolute_uri(url) if request else url


class FileBatchListSerializer(serializers.ModelSerializer):
    items_count = serializers.IntegerField(source="items.count", read_only=True)

    class Meta:
        model = FileBatch
        fields = [
            "id",
            "file_name",
            "seed_keyword",
            "title_count",
            "suggest_count",
            "desc_length",
            "created_at",
            "items_count",
        ]


class FileBatchSerializer(serializers.ModelSerializer):
    items = FileItemSerializer(many=True, read_only=True)

    class Meta:
        model = FileBatch
        fields = [
            "id",
            "file_name",
            "seed_keyword",
            "title_count",
            "suggest_count",
            "desc_length",
            "suggestions",
            "created_at",
            "items",
        ]


# ----------------------------
# Work & Claims (public/basic)
# ----------------------------
class WorkSerializer(serializers.ModelSerializer):
    class Meta:
        model = Work
        fields = [
            "id",
            "name",
            "file_batch",
            "price_per_item",
            "deadline_minutes",
            "total_slots",
            "remaining_slots",
            "created_at",
            "video_zip",
        ]


class WorkPublicListSerializer(serializers.ModelSerializer):
    file_name = serializers.CharField(source="file_batch.file_name", read_only=True)

    class Meta:
        model = Work
        fields = ["id", "name", "file_name", "remaining_slots", "price_per_item"]


class WorkClaimSerializer(serializers.ModelSerializer):
    file_item_title = serializers.SerializerMethodField()
    file_item_description = serializers.SerializerMethodField()
    file_item_tags = serializers.SerializerMethodField()
    video_url = serializers.SerializerMethodField()

    class Meta:
        model = WorkClaim
        fields = [
            "id",
            "user",
            "work",
            "file_item",
            "file_item_title",
            "file_item_description",
            "file_item_tags",
            "title",
            "description",
            "tags",
            "payout_amount",
            "status",
            "review_status",
            "youtube_url",
            "youtube_video_id",
            "yt_views",
            "yt_likes",
            "video_url",
        ]

    def get_file_item_title(self, obj):
        return obj.file_item.title if obj.file_item else ""

    def get_file_item_description(self, obj):
        return obj.file_item.description if obj.file_item else ""

    def get_file_item_tags(self, obj):
        return obj.file_item.tags if obj.file_item else ""

    def get_video_url(self, obj):
        if obj.work and obj.work.video_zip:
            try:
                return obj.work.video_zip.url
            except Exception:
                return None
        return None


# ----------------------------
# Wallet & Withdrawals
# ----------------------------
WR_RE = re.compile(r"WR#(\d+)")

class WalletSerializer(serializers.ModelSerializer):
    class Meta:
        model = Wallet
        fields = ["id", "balance"]


class WalletTransactionSerializer(serializers.ModelSerializer):
    ref_claim = WorkClaimSerializer(read_only=True)
    display_amount = serializers.SerializerMethodField()
    note = serializers.CharField(read_only=True)

    class Meta:
        model = WalletTransaction
        fields = ["id", "kind", "amount", "display_amount", "ref_claim", "note", "created_at"]

    def get_display_amount(self, obj):
        """
        User-friendly amount rules:
        - withdrawal_hold → absolute value (positive)
        - withdrawal → show WithdrawalRequest.amount if resolvable from note (WR#id)
        - default → obj.amount
        """
        kind = (obj.kind or "").lower()

        if kind == "withdrawal_hold":
            try:
                return f"{abs(obj.amount):.2f}"
            except Exception:
                return str(abs(obj.amount))

        if kind == "withdrawal":
            m = WR_RE.search(obj.note or "")
            if m:
                try:
                    wr = WithdrawalRequest.objects.get(pk=int(m.group(1)))
                    return f"{wr.amount:.2f}"
                except WithdrawalRequest.DoesNotExist:
                    pass
            try:
                return f"{obj.amount:.2f}"
            except Exception:
                return str(obj.amount)

        try:
            return f"{obj.amount:.2f}"
        except Exception:
            return str(obj.amount)


class WithdrawalRequestSerializer(serializers.ModelSerializer):
    class Meta:
        model = WithdrawalRequest
        fields = ["id", "amount", "upi_vpa", "status", "requested_at", "processed_at", "admin_note"]


# ----------------------------
# Auth
# ----------------------------
class RegisterSerializer(serializers.Serializer):
    email = serializers.EmailField()
    password = serializers.CharField(write_only=True)
    full_name = serializers.CharField(required=False, allow_blank=True)

    def validate_email(self, value):
        email = value.lower().strip()
        if User.objects.filter(username=email).exists() or User.objects.filter(email=email).exists():
            raise serializers.ValidationError("User with this email already exists.")
        return email

    def create(self, validated_data):
        email = validated_data["email"].lower().strip()
        password = validated_data["password"]
        full_name = validated_data.get("full_name", "").strip()
        user = User.objects.create_user(username=email, email=email, password=password)
        if hasattr(user, "first_name") and full_name:
            user.first_name = full_name
            user.save(update_fields=["first_name"])
        return user


class MeSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = ["id", "email", "username", "first_name", "last_name", "is_staff", "is_superuser"]


# ----------------------------
# Deep claim details / nested views
# ----------------------------
class FileItemDownloadSerializer(serializers.ModelSerializer):
    file_url = serializers.SerializerMethodField()

    class Meta:
        model = FileItem
        fields = [
            "id",
            "title",
            "description",
            "tags",
            "reuse_limit",
            "used_count",
            "is_used",
            "file_url",
        ]

    def get_file_url(self, obj):
        request = self.context.get("request")
        if hasattr(obj, "file") and obj.file:
            return request.build_absolute_uri(obj.file.url) if request else obj.file.url
        return None


class WorkDetailForClaimSerializer(serializers.ModelSerializer):
    video_zip_url = serializers.SerializerMethodField()

    class Meta:
        model = Work
        fields = ["id", "name", "price_per_item", "total_slots", "remaining_slots", "deadline_minutes", "video_zip_url"]

    def get_video_zip_url(self, obj):
        request = self.context.get("request")
        if obj.video_zip:
            try:
                url = obj.video_zip.url
            except Exception:
                return None
            return request.build_absolute_uri(url) if request else url
        return None


class WorkClaimDetailSerializer(serializers.ModelSerializer):
    file_item = FileItemDownloadSerializer(read_only=True)
    work = WorkDetailForClaimSerializer(read_only=True)

    class Meta:
        model = WorkClaim
        fields = [
            "id",
            "work",
            "file_item",
            "title",
            "description",
            "tags",
            "payout_amount",
            "status",
            "client_id",
            "assigned_at",
            "expires_at",
            "submitted_at",
            "youtube_url",
            "youtube_video_id",
            "yt_views",
            "yt_likes",
            "yt_last_checked_at",
            "next_check_at",
            "views_paid_units",
            "review_status",
        ]


class AdminClaimRowSerializer(serializers.ModelSerializer):
    user_email = serializers.SerializerMethodField()
    work_name = serializers.SerializerMethodField()

    class Meta:
        model = WorkClaim
        fields = [
            "id",
            "user_email",
            "work_name",
            "title",
            "description",
            "tags",
            "payout_amount",
            "status",
            "review_status",
            "youtube_url",
            "youtube_video_id",
            "yt_views",
            "yt_likes",
            "submitted_at",
            "assigned_at",
        ]

    def get_user_email(self, obj):
        return getattr(obj.user, "email", "") or getattr(obj.user, "username", "")

    def get_work_name(self, obj):
        return getattr(obj.work, "name", "")


# ----------------------------
# Admin / full views
# ----------------------------
class AdminFileItemSerializer(serializers.ModelSerializer):
    class Meta:
        model = FileItem
        fields = ["id", "title", "description", "tags", "reuse_limit", "used_count"]


class AdminFileBatchSerializer(serializers.ModelSerializer):
    items = AdminFileItemSerializer(many=True, read_only=True)

    class Meta:
        model = FileBatch
        fields = [
            "id",
            "file_name",
            "seed_keyword",
            "title_count",
            "suggest_count",
            "desc_length",
            "suggestions",
            "created_at",
            "items",
        ]


# ----------------------------
# Milestones
# ----------------------------
class MilestoneRuleSerializer(serializers.ModelSerializer):
    class Meta:
        model = MilestoneRule
        fields = ["id", "active", "threshold_views", "payout_amount", "created_at", "updated_at"]


class MilestoneRulePublicSerializer(serializers.ModelSerializer):
    class Meta:
        model = MilestoneRule
    # NOTE: public endpoint intentionally returns same fields as provided code
        fields = ["id", "active", "threshold_views", "payout_amount", "created_at", "updated_at"]


class MilestonePayoutSerializer(serializers.ModelSerializer):
    claim = WorkClaimSerializer(read_only=True)
    rule = MilestoneRuleSerializer(read_only=True)
    video_link = serializers.SerializerMethodField()

    class Meta:
        model = MilestonePayout
        fields = [
            "id",
            "claim",
            "rule",
            "views_snapshot",
            "likes_snapshot",
            "amount",
            "status",
            "created_at",
            "decided_at",
            "credited_txn",
            "video_link",
        ]

    def get_video_link(self, obj):
        c = obj.claim
        if getattr(c, "youtube_video_id", ""):
            return f"https://www.youtube.com/watch?v={c.youtube_video_id}"
        if getattr(c, "youtube_url", ""):
            return c.youtube_url
        return None
