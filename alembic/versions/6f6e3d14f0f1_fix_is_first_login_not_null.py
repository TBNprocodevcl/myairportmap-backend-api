"""fix_is_first_login_not_null

Revision ID: 6f6e3d14f0f1
Revises: 27dcac9bb310
Create Date: 2026-05-11 16:20:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '6f6e3d14f0f1'
down_revision: Union[str, Sequence[str], None] = '27dcac9bb310'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("UPDATE users SET is_first_login = FALSE WHERE is_first_login IS NULL")
    op.alter_column(
        'users',
        'is_first_login',
        existing_type=sa.Boolean(),
        nullable=False,
        server_default=sa.text('false'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'users',
        'is_first_login',
        existing_type=sa.Boolean(),
        nullable=True,
        server_default=None,
    )
