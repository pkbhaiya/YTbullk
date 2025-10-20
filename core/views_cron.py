# core/views_cron.py
from datetime import timedelta
from decimal import Decimal
import hmac

from django.conf import settings
from django.utils import timezone
from django.db.models import Q
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.permissions import IsAdminUser, IsAuthenticated, AllowAny
from rest_framework.response import Response
from rest_framework.pagination import PageNumberPagination

from .models import (
    SiteSettings,
    WorkClaim,
    ClaimMetricsLog,
    MilestoneRule,       # <-- requires the model added in models.py
    MilestonePayout,    # <-- requires the model added in models.py
    Wallet,
    WalletTransaction,
)
from .serializers import WorkClaimSerializer
from .utils_youtube import fetch_video_stats_batch

# =========================
# CONFIG
# =========================
CRON_SECRET = getattr(settings, "CRON_SECRET", None)  # set in env / settings.py
METRICS_COOLDOWN_DAYS = getattr(settings, "METRICS_COOLDOWN_DAYS", 5)  # days between checks
MAX_BATCH = 200  # max claims per cron run


class CronMetricsRefreshView(APIView):
    """
    GET /api/cron/metrics-refresh

    Security: requires X-CRON-SECRET header, or ?secret=... as a legacy fallback.
    Recommended: set CRON_SECRET in Django settings and call over HTTPS only.

    What it does:
      - Pulls a batch of claims that are due for a metrics refresh.
      - Updates yt_views / yt_likes.
      - Logs a snapshot in ClaimMetricsLog.
      - Schedules next_check_at = now + METRICS_COOLDOWN_DAYS.
      - If the claim is APPROVED and has a video (id or url), auto-creates
        MilestonePayout rows (status=pending_review) for any crossed rules.
        No credits are made here; admin approves later.
    """
    permission_classes = [AllowAny]  # we implement our own shared-secret guard below

    # -----------------------
    # Internal: secret check
    # -----------------------
    def _check_secret(self, request):
        """
        Return True if submitted secret matches CRON_SECRET using constant-time compare.
        Prefer header 'X-CRON-SECRET'. Query-param fallback allowed for backward compatibility
        but is not recommended (it may be logged by proxies).
        """
        if not CRON_SECRET:
            return False
        submitted = request.headers.get("X-CRON-SECRET") or request.query_params.get("secret")
        if not submitted:
            return False
        try:
            # constant-time compare to avoid timing attacks
            return hmac.compare_digest(str(submitted), str(CRON_SECRET))
        except Exception:
            return False

    # -------------
    # GET handler
    # -------------
    def get(self, request):
        # secret guard
        if not self._check_secret(request):
            # don't reveal details; just forbid
            return Response({"error": "forbidden"}, status=403)

        now = timezone.now()
        settings_row = SiteSettings.load() if hasattr(SiteSettings, "load") else SiteSettings.objects.first()
        if not settings_row or not getattr(settings_row, "youtube_api_key", None):
            return Response({"error": "YouTube API key not configured in SiteSettings"}, status=400)

        # Pick claims eligible for refresh
        qs = (
            WorkClaim.objects.filter(
                Q(youtube_video_id__gt="") &
                (Q(status="submitted") | Q(review_status__in=["approved", "pending_review"])) &
                (Q(next_check_at__lte=now) | Q(next_check_at__isnull=True))
            )
            .order_by("next_check_at")[:MAX_BATCH]
        )

        video_ids = [c.youtube_video_id for c in qs if c.youtube_video_id]
        if not video_ids:
            return Response({"updated": 0, "details": []})

        # fetch stats (returns dict mapping video_id -> {"views":..., "likes":...})
        try:
            stats = fetch_video_stats_batch(video_ids, settings_row.youtube_api_key, throttle_ms=250)
        except Exception as e:
            return Response({"error": f"YT fetch failed: {e}"}, status=500)

        updated = 0
        details = []

        # Process each claim (lock rows per-claim only where needed)
        for claim in qs:
            vid = claim.youtube_video_id
            if not vid or vid not in stats:
                continue
            s = stats[vid] or {}

            # defensively coerce to ints
            try:
                views = int(s.get("views") or 0)
            except Exception:
                views = 0
            try:
                likes = int(s.get("likes") or 0)
            except Exception:
                likes = 0

            # Update claim + schedule next check
            claim.yt_views = views
            claim.yt_likes = likes
            claim.yt_last_checked_at = now
            claim.next_check_at = now + timedelta(days=METRICS_COOLDOWN_DAYS)
            claim.save(update_fields=["yt_views", "yt_likes", "yt_last_checked_at", "next_check_at"])

            # Snapshot log
            ClaimMetricsLog.objects.create(
                claim=claim,
                views=claim.yt_views,
                likes=claim.yt_likes,
                snapshot_at=now,
            )

            # -------------------------------
            # NEW: Milestone detection block
            # -------------------------------
            # Only for COMPLETED work (approved claims), and where the video can be opened.
            if claim.review_status == "approved" and (claim.youtube_video_id or claim.youtube_url):
                # Find all active rules whose threshold <= current views
                rules = MilestoneRule.objects.filter(
                    active=True,
                    threshold_views__lte=claim.yt_views
                ).order_by("threshold_views")

                # Create a pending milestone payout for each rule not yet recorded.
                # Use a short transaction to avoid race conditions if cron runs concurrently.
                for rule in rules:
                    with transaction.atomic():
                        exists = MilestonePayout.objects.select_for_update().filter(
                            claim=claim, rule=rule
                        ).exists()
                        if not exists:
                            MilestonePayout.objects.create(
                                claim=claim,
                                rule=rule,
                                views_snapshot=claim.yt_views,
                                likes_snapshot=claim.yt_likes,
                                amount=rule.payout_amount,
                                status="pending_review",
                            )

            updated += 1
            details.append({
                "claim_id": claim.id,
                "video_id": vid,
                "views": claim.yt_views,
                "likes": claim.yt_likes,
                "next_check_at": claim.next_check_at,
            })

        return Response({"updated": updated, "details": details})


class AdminApprovedSubmissionsView(APIView):
    """
    GET /api/review/submissions
    Admin-only list of approved claims with a valid video reference,
    sorted by views/likes and recency of last check.
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = (
            WorkClaim.objects
            .filter(
                review_status="approved"
            )
            .filter(  # must have either a video_id or a url
                Q(youtube_video_id__isnull=False, youtube_video_id__gt="") |
                Q(youtube_url__isnull=False, youtube_url__gt="")
            )
            .order_by("-yt_views", "-yt_likes", "-yt_last_checked_at")[:500]
        )
        data = WorkClaimSerializer(qs, many=True).data
        return Response(data)


class MyApprovedClaimsView(APIView):
    """
    GET /api/my/claims
    Authenticated user’s own approved claims, sorted by views desc.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = (
            WorkClaim.objects
            .filter(user=request.user, review_status="approved", youtube_video_id__gt="")
            .order_by("-yt_views", "-yt_likes", "-yt_last_checked_at")[:500]
        )
        data = WorkClaimSerializer(qs, many=True).data
        return Response(data)
