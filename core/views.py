from typing import List
from decimal import Decimal
import random
from datetime import timedelta
import os

from django.conf import settings
from django.contrib.auth import get_user_model
from django.db import transaction
from django.db.models import F, Q
from django.http import FileResponse, Http404
from django.shortcuts import get_object_or_404
from django.utils import timezone

from rest_framework import status
from rest_framework.pagination import PageNumberPagination
from rest_framework.parsers import MultiPartParser, FormParser
from rest_framework.permissions import (
    AllowAny,
    IsAuthenticated,
    IsAdminUser,
    IsAuthenticatedOrReadOnly,
)
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework_simplejwt.tokens import RefreshToken

from .models import (
    SiteSettings,
    FileBatch,
    FileItem,
    Wallet,
    WithdrawalRequest,
    Work,
    WorkClaim,
    WalletTransaction,
    MilestoneRule,
)
from .pagination import StandardResultsSetPagination  # NOTE: imported earlier but unused; kept only if you actually use it elsewhere
from .serializers import (
    RegisterSerializer,
    MeSerializer,
    SettingsSerializer,
    FileBatchSerializer,
    FileBatchListSerializer,
    AdminFileBatchSerializer,
    WorkSerializer,
    WorkPublicListSerializer,
    WorkClaimSerializer,
    WithdrawalRequestSerializer,
    WalletTransactionSerializer,
    AdminClaimRowSerializer,
    MilestoneRulePublicSerializer,
    WorkClaimDetailSerializer,
)
from .utils_youtube import fetch_youtube_titles
from .utils_tags import (
    fetch_suggestions,
    generate_tags_from_snapshot_char_limit,
    generate_tags_per_title_using_random_title_seeds_with_char_limit,
)
from .utils_openai import generate_all_descriptions, extract_global_keywords_from_titles


User = get_user_model()


def _clamp(val, lo, hi):
    try:
        n = int(val)
    except Exception:
        n = lo
    return max(lo, min(hi, n))


# ---------- Settings ----------
class SettingsView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        s = SiteSettings.load()
        return Response(SettingsSerializer(s).data)

    def put(self, request):
        s = SiteSettings.load()
        ser = SettingsSerializer(s, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        ser.save()
        return Response(SettingsSerializer(s).data)


# ---------- Files ----------
class FileListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = FileBatch.objects.order_by("-created_at")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = FileBatchListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class FileDetailView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request, batch_id):
        try:
            b = FileBatch.objects.get(id=batch_id)
        except FileBatch.DoesNotExist:
            return Response({"error": "file not found"}, status=404)
        remaining_capacity = sum(max(0, it.reuse_limit - it.used_count) for it in b.items.all())
        data = FileBatchSerializer(b).data
        data["reuse_capacity"] = remaining_capacity
        return Response(data)


class FileCapacityView(APIView):
    permission_classes = [IsAdminUser]
    """Quick preview of how many total tasks this file can still support (reuse limit included)."""

    def get(self, request, batch_id):
        try:
            b = FileBatch.objects.get(id=batch_id)
        except FileBatch.DoesNotExist:
            return Response({"error": "file not found"}, status=404)
        remaining_capacity = sum(max(0, it.reuse_limit - it.used_count) for it in b.items.all())
        return Response(
            {
                "file_id": b.id,
                "file_name": b.file_name,
                "items_total": b.items.count(),
                "reuse_limit_per_item": 2,
                "remaining_capacity": remaining_capacity,
            }
        )


# ---------- Works ----------
class WorkCreateFromFileView(APIView):
    permission_classes = [IsAdminUser]
    """
    POST /api/works/create_from_file  (multipart)
    fields:
      file_id, name, price_per_item, total_works, [deadline_minutes>=60 default 60], video_zip (file)
    """
    parser_classes = [MultiPartParser, FormParser]

    def post(self, request):
        file_id = request.data.get("file_id")
        name = (request.data.get("name") or "").strip()
        price_per_item = request.data.get("price_per_item", "10.00")
        total_works = _clamp(request.data.get("total_works", 1), 1, 1000000)
        deadline = _clamp(request.data.get("deadline_minutes", 60), 60, 100000)
        video_zip = request.data.get("video_zip")

        if not (file_id and name and video_zip):
            return Response({"error": "file_id, name and video_zip are required"}, status=400)

        try:
            fb = FileBatch.objects.get(id=file_id)
        except FileBatch.DoesNotExist:
            return Response({"error": "file not found"}, status=404)

        remaining_capacity = sum(max(0, it.reuse_limit - it.used_count) for it in fb.items.all())
        if total_works > remaining_capacity:
            return Response(
                {
                    "error": f"Requested {total_works} works exceeds remaining capacity {remaining_capacity} for this file."
                },
                status=400,
            )

        with transaction.atomic():
            w = Work.objects.create(
                file_batch=fb,
                name=name,
                price_per_item=price_per_item,
                deadline_minutes=deadline,
                video_zip=video_zip,
                total_slots=total_works,
                remaining_slots=total_works,
            )
        return Response(WorkSerializer(w).data)


class WorkPublicListView(APIView):
    def get(self, request):
        qs = Work.objects.filter(remaining_slots__gt=0).order_by("-id")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = WorkPublicListSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


class WorkDetailView(APIView):
    def get(self, request, work_id):
        try:
            w = Work.objects.get(id=work_id)
        except Work.DoesNotExist:
            return Response({"error": "work not found"}, status=404)
        return Response(WorkSerializer(w).data)


# ---------- Claims (AUTH-ONLY) ----------
class WorkClaimCreateView(APIView):
    """
    POST /api/works/<int:work_id>/claim
    Auth required.
    Rules:
      - A user may have at most ONE active claim at a time (status='claimed' and not expired).
      - A user may NEVER participate in the same work twice (any status), enforced in code + DB constraint.
      - Randomly selects a FileItem that has not reached its reuse_limit.
      - Decrements Work.remaining_slots and increments FileItem.used_count atomically.
      - Sets expires_at = now + work.deadline_minutes.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, work_id: int):
        user = request.user
        now = timezone.now()

        existing_active = WorkClaim.objects.filter(
            user=user, status="claimed", expires_at__gt=now
        ).first()
        if existing_active:
            return Response(
                {
                    "error": "You already have an active task.",
                    "active_claim": {
                        "id": existing_active.id,
                        "work_id": existing_active.work_id,
                        "title": existing_active.title,
                        "description": existing_active.description,
                        "tags": existing_active.tags,
                        "expires_at": existing_active.expires_at,
                        "status": existing_active.status,
                    },
                },
                status=400,
            )

        if WorkClaim.objects.filter(user=user, work_id=work_id).exists():
            return Response({"error": "You have already participated in this work."}, status=400)

        try:
            w = Work.objects.get(id=work_id)
        except Work.DoesNotExist:
            return Response({"error": "work not found"}, status=404)

        expires_at = now + timedelta(minutes=w.deadline_minutes or 60)

        with transaction.atomic():
            w = Work.objects.select_for_update().get(id=work_id)

            if w.remaining_slots <= 0:
                return Response({"error": "This work is sold out"}, status=400)

            candidate_ids = list(
                w.file_batch.items.filter(used_count__lt=F("reuse_limit")).values_list("id", flat=True)
            )
            if not candidate_ids:
                return Response({"error": "No more available metadata items in this file"}, status=400)

            random.shuffle(candidate_ids)
            chosen_id = candidate_ids[0]

            fi = FileItem.objects.select_for_update().get(id=chosen_id)

            FileItem.objects.filter(id=fi.id).update(used_count=F("used_count") + 1)
            Work.objects.filter(id=w.id).update(remaining_slots=F("remaining_slots") - 1)

            claim = WorkClaim.objects.create(
                work=w,
                file_item=fi,
                user=user,
                title=fi.title,
                description=fi.description,
                tags=fi.tags,
                payout_amount=w.price_per_item,
                status="claimed",
                expires_at=expires_at,
            )

        claim.refresh_from_db()
        w.refresh_from_db()
        fi.refresh_from_db()

        return Response(
            {
                "work": {
                    "id": w.id,
                    "name": w.name,
                    "price_per_item": str(w.price_per_item),
                    "remaining_slots": w.remaining_slots,
                    "deadline_minutes": w.deadline_minutes,
                },
                "claim": {
                    "id": claim.id,
                    "work_id": claim.work_id,
                    "file_item_id": claim.file_item_id,
                    "title": claim.title,
                    "description": claim.description,
                    "tags": claim.tags,
                    "payout_amount": str(claim.payout_amount),
                    "status": claim.status,
                    "assigned_at": claim.assigned_at,
                    "expires_at": claim.expires_at,
                },
            },
            status=200,
        )


class WorkClaimSubmitView(APIView):
    """
    POST /api/claims/<claim_id>/submit
    body: { youtube_url }
    (JWT required)
    """
    permission_classes = [IsAuthenticated]

    def post(self, request, claim_id):
        youtube_url = (request.data.get("youtube_url") or "").strip()
        if not youtube_url:
            return Response({"error": "youtube_url is required"}, status=400)

        from urllib.parse import urlparse

        def is_youtube_url(url):
            try:
                p = urlparse(url)
                host = (p.hostname or "").lower()
                if "youtube.com" in host or host == "youtu.be":
                    return True
                return False
            except Exception:
                return False

        if not is_youtube_url(youtube_url):
            return Response({"error": "Only YouTube URLs accepted."}, status=400)

        try:
            c = WorkClaim.objects.select_for_update().get(id=claim_id, user=request.user)
        except WorkClaim.DoesNotExist:
            return Response({"error": "claim not found"}, status=404)

        if c.status not in ("claimed", "submitted"):
            return Response({"error": f"cannot submit when status={c.status}"}, status=400)

        with transaction.atomic():
            c.youtube_url = youtube_url
            c.submitted_at = timezone.now()
            c.status = "submitted"
            c.save(update_fields=["youtube_url", "submitted_at", "status"])

        return Response({"success": True, "message": "Work submitted successfully."})


class WorkSweepExpireView(APIView):
    """
    POST /api/works/<work_id>/sweep_expired
    - Expires all 'claimed' whose expires_at <= now
    - For each expired: remaining_slots += 1; file_item.used_count -= 1; status='expired'
    """
    def post(self, request, work_id):
        try:
            w = Work.objects.select_for_update().get(id=work_id)
        except Work.DoesNotExist:
            return Response({"error": "work not found"}, status=404)

        now = timezone.now()
        with transaction.atomic():
            w = Work.objects.select_for_update().get(id=work_id)
            to_expire = list(
                WorkClaim.objects.select_for_update().filter(
                    work=w, status="claimed", expires_at__lte=now
                )
            )
            count = 0
            for c in to_expire:
                FileItem.objects.filter(id=c.file_item_id).update(used_count=F("used_count") - 1)
                Work.objects.filter(id=w.id).update(remaining_slots=F("remaining_slots") + 1)
                c.status = "expired"
                c.save(update_fields=["status"])
                count += 1

        w.refresh_from_db()
        return Response({"expired": count, "remaining_slots": w.remaining_slots})


# ---------- Auth ----------
class RegisterView(APIView):
    permission_classes = [AllowAny]
    """
    POST /api/auth/register
    { "email": "...", "password": "...", "full_name": "..." }
    """

    def post(self, request):
        ser = RegisterSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        user = ser.save()
        return Response(MeSerializer(user).data, status=201)


class MeView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        return Response(MeSerializer(request.user).data)


class WorkClaimActiveViewAuth(APIView):
    """
    GET /api/claims/active_auth   (JWT required)
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        now = timezone.now()
        c = (
            WorkClaim.objects.filter(user=request.user, status="claimed", expires_at__gt=now)
            .order_by("-assigned_at")
            .first()
        )
        return Response({"active": WorkClaimSerializer(c).data if c else None})


# ---------- Admin: Withdrawals ----------
class AdminWithdrawListView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        status_q = request.query_params.get("status", "pending")
        qs = WithdrawalRequest.objects.all()
        if status_q:
            qs = qs.filter(status=status_q)
        qs = qs.order_by("-requested_at")

        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = WithdrawalRequestSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


# ---------- Wallet ----------
class MyWalletTransactionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet = Wallet.get_or_create_for_user(request.user)
        qs = wallet.transactions.all().order_by("-created_at")
        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        serializer = WalletTransactionSerializer(page, many=True)
        return paginator.get_paginated_response(serializer.data)


# ---------- Admin: Claim review ----------
class AdminApproveClaimView(APIView):
    """
    POST /api/admin/claims/<claim_id>/approve
    Idempotent: if already approved, does nothing.
    Credits wallet exactly once with claim.payout_amount.
    """
    permission_classes = [IsAdminUser]

    def post(self, request, claim_id):
        try:
            with transaction.atomic():
                claim = WorkClaim.objects.select_for_update().get(pk=claim_id)
                if claim.review_status == "approved":
                    return Response({"ok": True, "detail": "Already approved"})

                claim.review_status = "approved"
                claim.save(update_fields=["review_status"])

                wallet = Wallet.get_or_create_for_user(claim.user)
                already = wallet.transactions.filter(kind="task_credit", ref_claim=claim).exists()
                if not already:
                    amount = claim.payout_amount or Decimal("0")
                    WalletTransaction.apply_transaction(
                        wallet,
                        "task_credit",
                        amount,
                        ref_claim=claim,
                        note=f"Approved claim #{claim.id}",
                    )
                return Response({"ok": True})
        except WorkClaim.DoesNotExist:
            return Response({"error": "Claim not found"}, status=404)


class AdminRejectClaimView(APIView):
    """
    POST /api/admin/claims/<claim_id>/reject
    Sets review_status to rejected. Does NOT credit wallet.
    """
    permission_classes = [IsAdminUser]

    def post(self, request, claim_id):
        try:
            with transaction.atomic():
                claim = WorkClaim.objects.select_for_update().get(pk=claim_id)
                if claim.review_status == "rejected":
                    return Response({"ok": True, "detail": "Already rejected"})

                claim.review_status = "rejected"
                claim.save(update_fields=["review_status"])
                return Response({"ok": True})
        except WorkClaim.DoesNotExist:
            return Response({"error": "Claim not found"}, status=404)


class AdminSubmissionQueueView(APIView):
    """
    GET /api/review/submissions
    Admin-only. Shows all user-submitted work so it can be reviewed.
    Optional query params:
      - status=submitted|claimed|expired   (default: submitted)
      - review=pending_review|approved|rejected  (default: any)
      - search=<text>  (matches title/description/user/work)
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        status_filter = request.query_params.get("status", "submitted").strip().lower()
        review_filter = (request.query_params.get("review") or "").strip().lower()
        search = (request.query_params.get("search") or "").strip()

        qs = WorkClaim.objects.all()

        if status_filter:
            qs = qs.filter(status=status_filter)

        if review_filter in {"pending_review", "approved", "rejected"}:
            qs = qs.filter(review_status=review_filter)

        qs = qs.filter(
            Q(youtube_video_id__isnull=False, youtube_video_id__gt="")
            | Q(youtube_url__isnull=False, youtube_url__gt="")
        )

        if search:
            qs = qs.filter(
                Q(title__icontains=search)
                | Q(description__icontains=search)
                | Q(tags__icontains=search)
                | Q(user__email__icontains=search)
                | Q(user__username__icontains=search)
                | Q(work__name__icontains=search)
            )

        qs = qs.order_by("-submitted_at", "-yt_views", "-yt_likes")

        paginator = PageNumberPagination()
        page = paginator.paginate_queryset(qs, request, view=self)
        data = AdminClaimRowSerializer(page, many=True).data
        return paginator.get_paginated_response(data)


# ---------- Claims: details & lists ----------
class ClaimDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request, claim_id):
        try:
            claim = WorkClaim.objects.get(id=claim_id, user=request.user)
        except WorkClaim.DoesNotExist:
            return Response({"error": "claim not found or no access"}, status=status.HTTP_404_NOT_FOUND)

        ser = WorkClaimDetailSerializer(claim, context={"request": request})
        return Response(ser.data)


class MyClaimsAllView(APIView):
    """
    GET /api/claims/mine
    Returns all claims for the authenticated user (any status).
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = WorkClaim.objects.filter(user=request.user)
        serializer = WorkClaimSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data)


class MyClaimsAPIView(APIView):
    """
    GET /api/claims/active
    Returns active claims for the authenticated user.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        qs = WorkClaim.objects.filter(user=request.user, status="claimed")
        serializer = WorkClaimSerializer(qs, many=True, context={"request": request})
        return Response(serializer.data)


# ---------- File downloads ----------
class FileDownloadView(APIView):
    permission_classes = [IsAuthenticatedOrReadOnly]

    def get(self, request, item_id):
        try:
            fi = FileItem.objects.get(pk=item_id)
        except FileItem.DoesNotExist:
            raise Http404("File not found")
        if not fi.video_file:
            raise Http404("No file associated")

        file_path = fi.video_file.path
        filename = os.path.basename(file_path)
        response = FileResponse(open(file_path, "rb"), as_attachment=True, filename=filename)
        return response  # NOTE: return added for completeness; no logic change intended elsewhere


# ---------- Auth: logout ----------
class LogoutView(APIView):
    """
    POST /api/auth/logout
    Body: { "refresh": "<refresh_token>" }
    Blacklists the provided refresh token (so it cannot be used again).
    """
    permission_classes = [AllowAny]

    def post(self, request):
        token = request.data.get("refresh", None)
        if not token:
            return Response({"error": "refresh token required"}, status=status.HTTP_400_BAD_REQUEST)
        try:
            RefreshToken(token).blacklist()
        except Exception:
            return Response({"ok": True, "note": "token invalid or already blacklisted"}, status=200)
        return Response({"ok": True}, status=200)


# ---------- Admin: file generation ----------
class GenerateAndSaveFileView(APIView):
    permission_classes = [IsAdminUser]
    """
    POST /api/files/generate
    body: {
      file_name, keyword, title_count, suggest_count, desc_length,
      [tag_char_limit=400]
      [tag_word_quota=400]  // legacy alias for char limit (deprecated)
    }
    """

    def post(self, request):
        file_name = (request.data.get("file_name") or "").strip()
        keyword = (request.data.get("keyword") or "").strip()
        title_count = _clamp(request.data.get("title_count", 10), 1, 100)
        suggest_count = _clamp(request.data.get("suggest_count", 20), 1, 100)
        desc_length = _clamp(request.data.get("desc_length", 200), 50, 2000)

        if "tag_char_limit" in request.data:
            tag_char_limit = _clamp(request.data.get("tag_char_limit"), 50, 2000)
        else:
            tag_char_limit = _clamp(request.data.get("tag_word_quota", 400), 50, 2000)

        if not file_name or not keyword:
            return Response({"error": "file_name and keyword are required"}, status=400)

        s = SiteSettings.load()
        if not s.youtube_api_key:
            return Response({"error": "YouTube API key not set (PUT /api/settings/)"}, status=400)
        if not s.openai_api_key:
            return Response({"error": "OpenAI API key not set (PUT /api/settings/)"}, status=400)

        try:
            titles: List[str] = fetch_youtube_titles(keyword, title_count, api_key=s.youtube_api_key)
        except Exception as e:
            return Response({"error": f"YouTube fetch failed: {e}"}, status=400)

        if not titles:
            return Response({"error": "No Shorts titles found for India"}, status=404)

        global_keywords: List[str] = extract_global_keywords_from_titles(
            titles, max_unigrams=80, max_bigrams=80, min_len=3
        )

        suggestions_snapshot = fetch_suggestions(keyword, suggest_count)

        try:
            descriptions = generate_all_descriptions(
                openai_api_key=s.openai_api_key,
                titles=titles,
                global_keywords=global_keywords,
                desc_len=desc_length,
                strip_emojis=True,
                model="gpt-4o-mini",
                temperature=0.7,
                max_tokens=16000,
                batch_size=4,
                max_retries=2,
            )
        except Exception as e:
            return Response({"error": f"Description generation failed: {e}"}, status=400)

        tags_lines = generate_tags_from_snapshot_char_limit(
            suggestions_snapshot=suggestions_snapshot,
            n_items=len(titles),
            char_limit=tag_char_limit,
            global_seed=None,
        )

        if any(not t for t in tags_lines):
            per_title = generate_tags_per_title_using_random_title_seeds_with_char_limit(
                titles=titles,
                suggest_count=suggest_count,
                char_limit=tag_char_limit,
                global_seed=None,
            )
            tags_lines = [t if t else per_title[i] for i, t in enumerate(tags_lines)]

        with transaction.atomic():
            if FileBatch.objects.filter(file_name=file_name).exists():
                return Response(
                    {"error": "file_name already exists. Choose a unique name."}, status=400
                )

            batch = FileBatch.objects.create(
                file_name=file_name,
                seed_keyword=keyword,
                title_count=title_count,
                suggest_count=suggest_count,
                desc_length=desc_length,
                suggestions=suggestions_snapshot,
            )

            items = []
            for i, t in enumerate(titles):
                items.append(
                    FileItem(
                        batch=batch,
                        title=t,
                        description=(descriptions[i] if i < len(descriptions) else ""),
                        tags=(tags_lines[i] if i < len(tags_lines) else ""),
                    )
                )
            FileItem.objects.bulk_create(items)

        return Response(AdminFileBatchSerializer(batch).data, status=200)


# ---------- Public: milestones ----------
class PublicMilestoneRulesView(APIView):
    """
    GET /api/public/milestones
    Read-only. No auth. Returns only ACTIVE rules, sorted by threshold_views asc.
    Nothing else is exposed here.
    """
    permission_classes = [AllowAny]

    def get(self, request):
        qs = (
            MilestoneRule.objects.filter(active=True)
            .order_by("threshold_views")
            .only("id", "active", "threshold_views", "payout_amount", "created_at", "updated_at")
        )
        return Response(MilestoneRulePublicSerializer(qs, many=True).data)




class AdminUserStatsView(APIView):
    """
    GET /api/admin/users/stats
    Optional query param: ?email=<email>
    Returns: { total_users: int, user: MeSerializer | null }
    Admin-only.
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        User = get_user_model()
        total = User.objects.count()

        email = (request.query_params.get("email") or "").strip().lower()
        user_data = None
        if email:
            # try by email first, then username (since you use username==email)
            u = (User.objects.filter(email__iexact=email).first()
                 or User.objects.filter(username__iexact=email).first())
            if u:
                user_data = MeSerializer(u).data

        return Response({"total_users": total, "user": user_data})