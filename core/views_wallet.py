# core/views_wallet.py
from decimal import Decimal
from django.utils import timezone
from django.db import transaction as db_txn
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated, IsAdminUser


from .models import SiteSettings, Wallet, WalletTransaction, WithdrawalRequest
from .serializers import WalletSerializer, WalletTransactionSerializer, WithdrawalRequestSerializer

# ===== USER ENDPOINTS =====

class MyWalletView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet = Wallet.get_or_create_for_user(request.user)
        settings_row = SiteSettings.objects.first()
        min_withdraw = settings_row.min_withdraw_amount if settings_row else Decimal("0")
        data = WalletSerializer(wallet).data
        data["min_withdraw_amount"] = str(min_withdraw)
        return Response(data)


class MyWalletTransactionsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        wallet = Wallet.get_or_create_for_user(request.user)
        txns = wallet.transactions.all().order_by("-created_at")[:200]
        return Response(WalletTransactionSerializer(txns, many=True).data)


class MyWithdrawRequestView(APIView):
    """
    POST body: {"amount": "500.00", "upi_vpa":"name@bank"}
    Creates a withdrawal *hold* immediately (negative txn).
    Admin will later approve or reject.
    """
    permission_classes = [IsAuthenticated]

    def post(self, request):
        amount = Decimal(str(request.data.get("amount", "0")) or "0")
        upi_vpa = (request.data.get("upi_vpa") or "").strip()
        if amount <= 0 or not upi_vpa:
            return Response({"error": "Invalid amount or UPI VPA."}, status=400)

        settings_row = SiteSettings.objects.first()
        min_withdraw = settings_row.min_withdraw_amount if settings_row else Decimal("0")

        wallet = Wallet.get_or_create_for_user(request.user)

        # enforce min and available balance
        if amount < min_withdraw:
            return Response({"error": f"Minimum withdrawal is {min_withdraw}."}, status=400)
        if wallet.balance < amount:
            return Response({"error": "Insufficient wallet balance."}, status=400)

        with db_txn.atomic():
            # Create pending request
            req = WithdrawalRequest.objects.create(wallet=wallet, amount=amount, upi_vpa=upi_vpa, status="pending")
            # Place a hold (debit)
            WalletTransaction.apply_transaction(wallet, "withdrawal_hold", -amount, ref_claim=None, note=f"Hold for WR#{req.pk}")

        return Response(WithdrawalRequestSerializer(req).data, status=201)


# ===== ADMIN ENDPOINTS =====
# In production, lock these with admin-only permission classes

class AdminWithdrawListView(APIView):
    permission_classes = [IsAdminUser]  # TODO: replace with admin auth in prod

    def get(self, request):
        status_q = request.query_params.get("status", "pending")
        qs = WithdrawalRequest.objects.all()
        if status_q:
            qs = qs.filter(status=status_q)
        qs = qs.order_by("-requested_at")[:200]
        return Response(WithdrawalRequestSerializer(qs, many=True).data)


class AdminWithdrawApproveView(APIView):
    permission_classes = [IsAdminUser]  # TODO: replace with admin auth

    def post(self, request, pk):
        try:
            wr = WithdrawalRequest.objects.select_for_update().get(pk=pk)
        except WithdrawalRequest.DoesNotExist:
            return Response({"error": "Not found"}, status=404)
        if wr.status != "pending":
            return Response({"error": "Already processed"}, status=400)

        with db_txn.atomic():
            wr.status = "approved"
            wr.processed_at = timezone.now()
            wr.save(update_fields=["status", "processed_at"])

            # Convert the hold into a final withdrawal by adding a zero or separate txn?
            # Simpler: leave the hold (negative) as is and add a small note:
            WalletTransaction.apply_transaction(
                wr.wallet, "withdrawal", Decimal("0.00"), note=f"Approved WR#{wr.pk}"
            )

        return Response({"ok": True, "id": wr.pk, "status": wr.status})


class AdminWithdrawRejectView(APIView):
    permission_classes = [IsAdminUser]  # TODO: replace with admin auth

    def post(self, request, pk):
        try:
            wr = WithdrawalRequest.objects.select_for_update().get(pk=pk)
        except WithdrawalRequest.DoesNotExist:
            return Response({"error": "Not found"}, status=404)
        if wr.status != "pending":
            return Response({"error": "Already processed"}, status=400)

        with db_txn.atomic():
            wr.status = "rejected"
            wr.processed_at = timezone.now()
            wr.admin_note = (request.data.get("note") or "")[:255]
            wr.save(update_fields=["status", "processed_at", "admin_note"])

            # Release the hold by reversing it
            WalletTransaction.apply_transaction(
                wr.wallet, "reversal", wr.amount, note=f"Reversal of hold WR#{wr.pk}"
            )

        return Response({"ok": True, "id": wr.pk, "status": wr.status})
