# core/fields.py
from django.db import models
from decimal import Decimal

MoneyField = lambda **kw: models.DecimalField(
    max_digits=12, decimal_places=2, **kw
)

# usage in models.py
from .fields import MoneyField
min_withdraw_amount = MoneyField(default=Decimal("50.00"))
payout_amount = MoneyField(default=Decimal("5.00"))
