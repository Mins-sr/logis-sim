"""Add durable market, trip, and notification lifecycle state."""

from alembic import op
from sqlalchemy import Column, DateTime, Integer, MetaData, String, Table, inspect, select, text

from app import models  # noqa: F401 - register all mapped tables


revision = "0002_market_lifecycle"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def _has_column(inspector, table: str, column: str) -> bool:
    return any(item["name"] == column for item in inspector.get_columns(table))


def _has_index(inspector, table: str, name: str) -> bool:
    return any(item["name"] == name for item in inspector.get_indexes(table))


class HostMembershipIntegrityError(RuntimeError):
    """Raised before the HOST uniqueness index can hide legacy corruption."""


def validate_host_memberships(bind) -> None:
    inspector = inspect(bind)
    table_names = set(inspector.get_table_names())
    if not {"memberships", "users"}.issubset(table_names):
        return

    metadata = MetaData()
    memberships = Table("memberships", metadata, autoload_with=bind)
    users = Table("users", metadata, autoload_with=bind)
    rows = list(
        bind.execute(
            select(
                memberships.c.session_id.label("session_id"),
                memberships.c.id.label("membership_id"),
                memberships.c.user_id.label("user_id"),
                users.c.is_admin.label("user_is_admin"),
            )
            .select_from(memberships.outerjoin(users, memberships.c.user_id == users.c.id))
            .where(memberships.c.role == "HOST")
            .order_by(memberships.c.session_id, memberships.c.id)
        ).mappings()
    )
    by_session: dict[str, list[dict[str, object]]] = {}
    violations: list[str] = []
    for row in rows:
        by_session.setdefault(str(row["session_id"]), []).append(dict(row))
        if row["user_is_admin"] is not True:
            violations.append(f"non-administrator HOST membership={row['membership_id']} session={row['session_id']} user={row['user_id']}")
    for session_id, session_rows in by_session.items():
        if len(session_rows) > 1:
            memberships_list = ",".join(str(row["membership_id"]) for row in session_rows)
            violations.append(f"multiple HOST memberships session={session_id} memberships={memberships_list}")
    if violations:
        raise HostMembershipIntegrityError(
            "0002_market_lifecycle aborted before HOST uniqueness index: "
            + "; ".join(violations)
            + "; repair the session to exactly one administrator HOST and rerun the migration"
        )


def upgrade() -> None:
    bind = op.get_bind()
    inspector = inspect(bind)
    validate_host_memberships(bind)

    additions = {
        "memberships": [("next_notification_seq", Column("next_notification_seq", Integer(), nullable=False, server_default=text("0")))],
        "stream_notifications": [("event_id", Column("event_id", String(36), nullable=True))],
        "plans": [
            ("truck_id", Column("truck_id", String(36), nullable=True)),
            ("base_sim_time_ms", Column("base_sim_time_ms", Integer(), nullable=False, server_default=text("0"))),
            ("time_limit_ms", Column("time_limit_ms", Integer(), nullable=False, server_default=text("5000"))),
            ("calculation_deadline_at", Column("calculation_deadline_at", DateTime(timezone=True), nullable=True)),
        ],
    }
    for table, columns in additions.items():
        if table not in inspector.get_table_names():
            continue
        for name, column in columns:
            if not _has_column(inspector, table, name):
                op.add_column(table, column)

    inspector = inspect(bind)
    for table_name in ("trips", "resource_reservations"):
        if table_name not in inspector.get_table_names():
            models.Base.metadata.tables[table_name].create(bind=bind, checkfirst=True)

    inspector = inspect(bind)
    index_names = {
        "memberships": ("uq_membership_session_host",),
        "stream_notifications": ("ix_stream_notifications_event_id",),
        "plans": ("ix_plans_truck_id",),
        "trips": ("uq_active_trip_per_truck",),
        "resource_reservations": ("uq_active_resource_reservation",),
    }
    for table_name, names in index_names.items():
        if table_name not in inspector.get_table_names():
            continue
        table = models.Base.metadata.tables[table_name]
        for index in table.indexes:
            if index.name in names and not _has_index(inspector, table_name, index.name):
                index.create(bind=bind, checkfirst=True)

    # These are represented as unique indexes so the migration works on SQLite
    # as well as PostgreSQL without rewriting the initial migration.
    inspector = inspect(bind)
    unique_indexes = {
        "domain_events": ("uq_domain_event_session_seq", ("session_id", "event_seq")),
        "outbox": ("uq_outbox_event", ("event_id",)),
        "stream_notifications": ("uq_stream_event", ("session_id", "user_id", "event_id")),
    }
    for table_name, (name, columns) in unique_indexes.items():
        if table_name in inspector.get_table_names() and not _has_index(inspector, table_name, name):
            op.create_index(name, table_name, list(columns), unique=True)


def downgrade() -> None:
    raise NotImplementedError("market lifecycle migration is forward-only")
