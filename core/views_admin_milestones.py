from django.utils import timezone
from django.db import transaction
from rest_framework.views import APIView
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from rest_framework import status

from .models import MilestoneRule, MilestonePayout, Wallet, WalletTransaction
from .serializers import MilestoneRuleSerializer, MilestonePayoutSerializer

class AdminMilestoneRulesView(APIView):
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = MilestoneRule.objects.all().order_by("threshold_views")
        return Response(MilestoneRuleSerializer(qs, many=True).data)

    def post(self, request):
        ser = MilestoneRuleSerializer(data=request.data)
        ser.is_valid(raise_exception=True)
        obj = ser.save()
        return Response(MilestoneRuleSerializer(obj).data, status=201)

    def put(self, request):
        rid = request.data.get("id")
        try:
            obj = MilestoneRule.objects.get(pk=rid)
        except MilestoneRule.DoesNotExist:
            return Response({"error":"Not found"}, status=404)
        ser = MilestoneRuleSerializer(obj, data=request.data, partial=True)
        ser.is_valid(raise_exception=True)
        obj = ser.save()
        return Response(MilestoneRuleSerializer(obj).data)

class AdminMilestoneQueueView(APIView):
    """
    GET pending milestones for review (only those auto-created by cron).
    """
    permission_classes = [IsAdminUser]

    def get(self, request):
        qs = (MilestonePayout.objects
              .select_related("claim","rule","claim__user","claim__work")
              .filter(status="pending_review")
              .order_by("-views_snapshot","-created_at"))
        return Response(MilestonePayoutSerializer(qs, many=True).data)

class AdminMilestoneApproveView(APIView):
    """
    POST /api/admin/milestones/<pk>/approve
    Idempotent credit to user's wallet.
    """
    permission_classes = [IsAdminUser]

    def post(self, request, pk):
        try:
            with transaction.atomic():
                mp = MilestonePayout.objects.select_for_update().get(pk=pk)
                if mp.status == "approved" and mp.credited_txn_id:
                    return Response({"ok": True, "detail": "Already approved"})

                wallet = Wallet.get_or_create_for_user(mp.claim.user)

                # Defensive: check if already credited by note
                already = wallet.transactions.filter(
                    kind="milestone_bonus",
                    note__icontains=f"MilestonePayout#{mp.id}"
                ).first()
                if already:
                    mp.status = "approved"
                    mp.decided_at = timezone.now()
                    mp.credited_txn = already
                    mp.save(update_fields=["status","decided_at","credited_txn"])
                    return Response({"ok": True, "detail": "Already credited"})

                txn = WalletTransaction.apply_transaction(
                    wallet=wallet,
                    kind="milestone_bonus",
                    amount=mp.amount,
                    ref_claim=mp.claim,
                    note=f"MilestonePayout#{mp.id} - {mp.rule.threshold_views} views"
                )

                mp.status = "approved"
                mp.decided_at = timezone.now()
                mp.credited_txn = txn
                mp.save(update_fields=["status","decided_at","credited_txn"])
                return Response({"ok": True, "txn_id": txn.id})
        except MilestonePayout.DoesNotExist:
            return Response({"error":"Not found"}, status=404)

class AdminMilestoneRejectView(APIView):
    permission_classes = [IsAdminUser]

    def post(self, request, pk):
        try:
            with transaction.atomic():
                mp = MilestonePayout.objects.select_for_update().get(pk=pk)
                if mp.status == "rejected":
                    return Response({"ok": True, "detail": "Already rejected"})
                mp.status = "rejected"
                mp.decided_at = timezone.now()
                mp.save(update_fields=["status","decided_at"])
                return Response({"ok": True})
        except MilestonePayout.DoesNotExist:
            return Response({"error":"Not found"}, status=404)
