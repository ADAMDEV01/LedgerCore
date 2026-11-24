import logging
import secrets
from datetime import timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core import clock
from app.core.config import settings
from app.core.constants import SEQ_AUDIT, SEQ_INVOICE, SEQ_JOURNAL
from app.core.database import SessionLocal, set_tenant_context
from app.core.enums import AccountType, Role
from app.models import Account, DocumentSequence, JournalEntry, Organization, User
from app.services import invoices as invoice_svc
from app.services.auth import create_user
from app.services.ledger import LineInput, post_entry

log = logging.getLogger("ledgercore.seed")

CHART_OF_ACCOUNTS = [
    ("1000", "Operating Cash", AccountType.ASSET),
    ("1100", "Accounts Receivable", AccountType.ASSET),
    ("1500", "Equipment", AccountType.ASSET),
    ("2000", "Accounts Payable", AccountType.LIABILITY),
    ("2100", "Sales Tax Payable", AccountType.LIABILITY),
    ("3000", "Owner's Capital", AccountType.EQUITY),
    ("4000", "Service Revenue", AccountType.REVENUE),
    ("4900", "Realized FX Gain / (Loss)", AccountType.REVENUE),
    ("5000", "Cloud Infrastructure", AccountType.EXPENSE),
    ("5100", "Payroll & Contractors", AccountType.EXPENSE),
]


def ensure_sequences(db: Session) -> None:
    organization_id = int(db.info.get("organization_id", 1))
    for name in (SEQ_JOURNAL, SEQ_INVOICE, SEQ_AUDIT):
        if db.get(DocumentSequence, (organization_id, name)) is None:
            db.add(DocumentSequence(organization_id=organization_id, name=name, last_value=0))
    db.flush()


def ensure_chart(db: Session) -> None:
    existing = set(db.scalars(select(Account.code)))
    for code, name, kind in CHART_OF_ACCOUNTS:
        if code not in existing:
            db.add(Account(code=code, name=name, account_type=kind.value))
    db.flush()


def ensure_admin(db: Session) -> None:
    if db.scalar(select(func.count()).select_from(User)):
        return
    password = settings.INITIAL_ADMIN_PASSWORD or secrets.token_urlsafe(18)
    create_user(
        db,
        full_name="Chief Financial Officer",
        email=settings.INITIAL_ADMIN_EMAIL,
        password=password,
        role=Role.CFO.value,
        actor="system",
    )
    if not settings.INITIAL_ADMIN_PASSWORD:
        # Shown once, only because the operator did not choose a password.
        log.warning("Initial admin %s created with password: %s", settings.INITIAL_ADMIN_EMAIL, password)


def ensure_demo_viewer(db: Session) -> None:
    """Optionally provision a least-privilege demo login; never ships with default credentials."""
    if not settings.DEMO_VIEWER_EMAIL or not settings.DEMO_VIEWER_PASSWORD:
        return
    email = settings.DEMO_VIEWER_EMAIL.strip().lower()
    existing = db.scalar(select(User).where(User.email == email))
    if existing is not None:
        if existing.role != Role.VIEWER.value:
            raise RuntimeError("Configured demo account exists but is not a VIEWER")
        return
    create_user(
        db,
        full_name="LedgerCore Demo Viewer",
        email=email,
        password=settings.DEMO_VIEWER_PASSWORD,
        role=Role.VIEWER.value,
        actor="system",
    )


def _account_id(db: Session, code: str) -> int:
    return db.scalars(select(Account.id).where(Account.code == code)).one()


def seed_demo_data(db: Session) -> None:
    if db.scalar(select(func.count()).select_from(JournalEntry)):
        return
    today = clock.today()
    cash, capital = _account_id(db, "1000"), _account_id(db, "3000")
    cloud, payroll = _account_id(db, "5000"), _account_id(db, "5100")
    revenue = _account_id(db, "4000")
    actor = settings.INITIAL_ADMIN_EMAIL.lower()
    post_entry(
        db,
        entry_date=today - timedelta(days=75),
        description="Initial capital injection",
        lines=[LineInput(cash, debit=Decimal("100000.00")), LineInput(capital, credit=Decimal("100000.00"))],
        actor=actor,
    )
    for months_back, (rev, cloud_cost, pay) in enumerate(
        [("18500.00", "2100.00", "9000.00"), ("22400.00", "2350.00", "9000.00"), ("26800.00", "2600.00", "9500.00")]
    ):
        when = today - timedelta(days=30 * (2 - months_back) + 5)
        post_entry(
            db,
            entry_date=when,
            description="Monthly service revenue received",
            lines=[LineInput(cash, debit=Decimal(rev)), LineInput(revenue, credit=Decimal(rev))],
            actor=actor,
        )
        post_entry(
            db,
            entry_date=when,
            description="Cloud infrastructure bill",
            lines=[LineInput(cloud, debit=Decimal(cloud_cost)), LineInput(cash, credit=Decimal(cloud_cost))],
            actor=actor,
        )
        post_entry(
            db,
            entry_date=when,
            description="Payroll and contractors",
            lines=[LineInput(payroll, debit=Decimal(pay)), LineInput(cash, credit=Decimal(pay))],
            actor=actor,
        )
    invoice_svc.create_invoice(
        db,
        client_name="Northwind Traders",
        client_email="ap@northwind.example.org",
        currency="EUR",
        exchange_rate=Decimal("1.085000"),
        amount=Decimal("8200.00"),
        tax_rate=Decimal("14.00"),
        due_date=today + timedelta(days=21),
        actor=actor,
    )


def seed_database() -> None:
    with SessionLocal() as db:
        organization = db.get(Organization, 1)
        if organization is None:
            organization = Organization(name="Default Organization", slug="default")
            db.add(organization)
            db.flush()
        set_tenant_context(db, organization.id)
        ensure_sequences(db)
        ensure_chart(db)
        ensure_admin(db)
        ensure_demo_viewer(db)
        if settings.SEED_DEMO_DATA:
            seed_demo_data(db)
        db.commit()
