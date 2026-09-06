"""Persist structured economic-calendar values."""

from alembic import op
import sqlalchemy as sa


revision = "0002_calendar_event_data"
down_revision = "0001_initial_schema"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("news_events", sa.Column("calendar_data", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("news_events", "calendar_data")