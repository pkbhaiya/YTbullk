# core/urls.py
from django.urls import path
from rest_framework_simplejwt.views import TokenObtainPairView, TokenRefreshView

from .views import (
    SettingsView,
    GenerateAndSaveFileView,
    FileListView,
    FileDetailView,
    FileCapacityView,
    WorkCreateFromFileView,
    WorkPublicListView,
    WorkDetailView,
    WorkClaimCreateView,
    WorkClaimSubmitView,
    WorkSweepExpireView,
    RegisterView,
    WorkClaimActiveViewAuth,
    MeView,
    LogoutView,
    ClaimDetailView,
    MyClaimsAllView,
    FileDownloadView,
    AdminApproveClaimView,
    AdminSubmissionQueueView,
    AdminRejectClaimView,
    PublicMilestoneRulesView,
    AdminUserStatsView
)

from .views_admin_milestones import (
    AdminMilestoneRulesView,
    AdminMilestoneQueueView,
    AdminMilestoneApproveView,
    AdminMilestoneRejectView,
)

from .views_cron import (
    CronMetricsRefreshView,
    MyApprovedClaimsView,
)

from .views_wallet import (
    MyWalletView,
    MyWalletTransactionsView,
    MyWithdrawRequestView,
    AdminWithdrawListView,
    AdminWithdrawApproveView,
    AdminWithdrawRejectView,
)

urlpatterns = [
    # ---------- Auth ----------
    path("auth/register", RegisterView.as_view()),           # POST {email, password, full_name?}
    path("auth/login", TokenObtainPairView.as_view()),       # POST {email, password}
    path("auth/refresh", TokenRefreshView.as_view()),        # POST {refresh}
    path("auth/me", MeView.as_view()),                       # GET current user info
    path("auth/logout", LogoutView.as_view()),               # POST {refresh} to blacklist

    # ---------- Settings ----------
    path("settings/", SettingsView.as_view()),

    # ---------- Files ----------
    path("files/generate", GenerateAndSaveFileView.as_view()),
    path("files", FileListView.as_view()),
    path("files/<int:batch_id>", FileDetailView.as_view()),
    path("files/<int:batch_id>/capacity", FileCapacityView.as_view()),
    path("files/<int:item_id>/download", FileDownloadView.as_view()),

    # ---------- Claims ----------
    path("claims/mine", MyClaimsAllView.as_view()),          # all claims for auth user
    path("claims/<int:claim_id>", ClaimDetailView.as_view()),  # GET single claim (owner only)
    path("claims/<int:claim_id>/submit", WorkClaimSubmitView.as_view()),
    path("claims/active_auth", WorkClaimActiveViewAuth.as_view()),

    # ---------- Works ----------
    path("works/create_from_file", WorkCreateFromFileView.as_view()),
    path("works", WorkPublicListView.as_view()),
    path("works/<int:work_id>", WorkDetailView.as_view()),
    path("works/<int:work_id>/claim", WorkClaimCreateView.as_view()),
    path("works/<int:work_id>/sweep_expired", WorkSweepExpireView.as_view()),

    # ---------- Cron ----------
    path("cron/metrics-refresh", CronMetricsRefreshView.as_view()),

    # ---------- Admin: Claim review ----------
    path("review/submissions", AdminSubmissionQueueView.as_view()),  # GET (admin)
    path("admin/claims/<int:claim_id>/approve", AdminApproveClaimView.as_view()),  # POST (admin)
    path("admin/claims/<int:claim_id>/reject", AdminRejectClaimView.as_view()),    # POST (admin)
    path("my/claims", MyApprovedClaimsView.as_view()),  # user’s approved claims

    # ---------- Wallet (user) ----------
    path("wallet/me", MyWalletView.as_view()),
    path("wallet/me/transactions", MyWalletTransactionsView.as_view()),
    path("wallet/withdraw", MyWithdrawRequestView.as_view()),

    # ---------- Wallet (admin) ----------
    path("admin/withdrawals", AdminWithdrawListView.as_view()),
    path("admin/withdrawals/<int:pk>/approve", AdminWithdrawApproveView.as_view()),
    path("admin/withdrawals/<int:pk>/reject", AdminWithdrawRejectView.as_view()),

    # ---------- Milestones ----------
    path("admin/milestones/rules", AdminMilestoneRulesView.as_view()),             # GET/POST/PUT
    path("admin/milestones/queue", AdminMilestoneQueueView.as_view()),             # GET pending achievements
    path("admin/milestones/<int:pk>/approve", AdminMilestoneApproveView.as_view()),# POST approve
    path("admin/milestones/<int:pk>/reject", AdminMilestoneRejectView.as_view()),  # POST reject
    path("public/milestones", PublicMilestoneRulesView.as_view()), 
    path("admin/users/stats", AdminUserStatsView.as_view()),                # GET active rules
]
