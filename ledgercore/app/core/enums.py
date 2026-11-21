from enum import StrEnum


class Role(StrEnum):
    VIEWER = "VIEWER"
    ACCOUNTANT = "ACCOUNTANT"
    CFO = "CFO"


class AccountType(StrEnum):
    ASSET = "ASSET"
    LIABILITY = "LIABILITY"
    EQUITY = "EQUITY"
    REVENUE = "REVENUE"
    EXPENSE = "EXPENSE"


# Account types whose normal balance is a debit. Everything else is credit-normal.
DEBIT_NORMAL = frozenset({AccountType.ASSET, AccountType.EXPENSE})


class InvoiceStatus(StrEnum):
    ISSUED = "ISSUED"
    PARTIALLY_PAID = "PARTIALLY_PAID"
    PAID = "PAID"
    VOID = "VOID"


# Derived display status. It is computed from due_date and never stored.
OVERDUE = "OVERDUE"


class EntrySource(StrEnum):
    MANUAL = "MANUAL"
    INVOICE = "INVOICE"
    PAYMENT = "PAYMENT"
    REVERSAL = "REVERSAL"


class PeriodStatus(StrEnum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
