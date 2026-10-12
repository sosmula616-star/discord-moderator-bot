"""Initial schema

Revision ID: 001_initial
Revises: 
Create Date: 2026-10-12 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "001_initial"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # Enums
    punishment_type = postgresql.ENUM('ban_role', 'mute_voice', 'mute_text', 'warn', 'warning', name='punishmenttype', create_type=False)
    punishment_type.create(op.get_bind(), checkfirst=True)

    report_status = postgresql.ENUM('open', 'accepted', 'rejected', 'closed', name='reportstatus', create_type=False)
    report_status.create(op.get_bind(), checkfirst=True)

    button_style = postgresql.ENUM('primary', 'secondary', 'success', 'danger', name='buttonstyle', create_type=False)
    button_style.create(op.get_bind(), checkfirst=True)

    # guild_settings
    op.create_table(
        'guild_settings',
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('role_access_id', sa.BigInteger(), nullable=True),
        sa.Column('role_noaccess_id', sa.BigInteger(), nullable=True),
        sa.Column('role_male_id', sa.BigInteger(), nullable=True),
        sa.Column('role_female_id', sa.BigInteger(), nullable=True),
        sa.Column('role_ban_id', sa.BigInteger(), nullable=True),
        sa.Column('role_mute_voice_id', sa.BigInteger(), nullable=True),
        sa.Column('role_mute_text_id', sa.BigInteger(), nullable=True),
        sa.Column('role_warn1_id', sa.BigInteger(), nullable=True),
        sa.Column('role_warn2_id', sa.BigInteger(), nullable=True),
        sa.Column('role_warning_id', sa.BigInteger(), nullable=True),
        sa.Column('channel_ban_room_id', sa.BigInteger(), nullable=True),
        sa.Column('channel_reports_id', sa.BigInteger(), nullable=True),
        sa.Column('category_reports_id', sa.BigInteger(), nullable=True),
        sa.Column('category_logs_id', sa.BigInteger(), nullable=True),
        sa.Column('channel_log_reports_id', sa.BigInteger(), nullable=True),
        sa.Column('channel_log_punish_id', sa.BigInteger(), nullable=True),
        sa.Column('channel_log_verify_id', sa.BigInteger(), nullable=True),
        sa.Column('channel_log_panel_id', sa.BigInteger(), nullable=True),
        sa.Column('max_mute_minutes', sa.Integer(), nullable=False, server_default='1440'),
        sa.Column('auto_ban_warn_count', sa.Integer(), nullable=False, server_default='3'),
        sa.Column('auto_ban_days', sa.Integer(), nullable=False, server_default='30'),
        sa.Column('report_limit_per_hour', sa.Integer(), nullable=False, server_default='5'),
        sa.Column('updated_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('guild_id')
    )

    # button_config
    op.create_table(
        'button_config',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('action_key', sa.String(length=64), nullable=False),
        sa.Column('label', sa.String(length=80), nullable=False),
        sa.Column('emoji', sa.String(length=32), nullable=True),
        sa.Column('style', sa.Enum('primary', 'secondary', 'success', 'danger', name='buttonstyle'), nullable=False, server_default='secondary'),
        sa.Column('order', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('enabled', sa.Boolean(), nullable=False, server_default='true'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('guild_id', 'action_key', name='uq_button_guild_action')
    )
    op.create_index('ix_button_config_guild_id', 'button_config', ['guild_id'])

    # button_roles
    op.create_table(
        'button_roles',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('button_id', sa.Integer(), nullable=False),
        sa.Column('role_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['button_id'], ['button_config.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # users
    op.create_table(
        'users',
        sa.Column('id', sa.BigInteger(), nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('username', sa.String(length=64), nullable=True),
        sa.Column('display_name', sa.String(length=64), nullable=True),
        sa.Column('avatar_url', sa.String(length=512), nullable=True),
        sa.Column('gender', sa.String(length=16), nullable=True),
        sa.Column('access_status', sa.String(length=16), nullable=True),
        sa.Column('first_seen', sa.DateTime(timezone=True), nullable=True),
        sa.Column('joined_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('left_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('id', 'guild_id', name='uq_user_guild')
    )
    op.create_index('ix_users_guild_id', 'users', ['guild_id'])

    # punishments
    op.create_table(
        'punishments',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('type', sa.Enum('ban_role', 'mute_voice', 'mute_text', 'warn', 'warning', name='punishmenttype'), nullable=False),
        sa.Column('moderator_id', sa.BigInteger(), nullable=False),
        sa.Column('reason', sa.String(length=512), nullable=False),
        sa.Column('description', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('expires_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('active', sa.Boolean(), nullable=False, server_default='true'),
        sa.Column('revoked_by', sa.BigInteger(), nullable=True),
        sa.Column('revoked_at', sa.DateTime(timezone=True), nullable=True),
        sa.Column('revoke_reason', sa.String(length=512), nullable=True),
        sa.Column('auto', sa.Boolean(), nullable=False, server_default='false'),
        sa.ForeignKeyConstraint(['user_id'], ['users.id']),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_punishments_guild', 'punishments', ['guild_id'])
    op.create_index('ix_punishments_user_active', 'punishments', ['user_id', 'active'])
    op.create_index('ix_punishments_expires', 'punishments', ['expires_at', 'active'])

    # role_backups
    op.create_table(
        'role_backups',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('punishment_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('role_id', sa.BigInteger(), nullable=False),
        sa.ForeignKeyConstraint(['punishment_id'], ['punishments.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_role_backups_user_id', 'role_backups', ['user_id'])

    # reports
    op.create_table(
        'reports',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('plaintiff_id', sa.BigInteger(), nullable=False),
        sa.Column('defendant_id', sa.BigInteger(), nullable=False),
        sa.Column('text', sa.Text(), nullable=False),
        sa.Column('evidence', sa.Text(), nullable=True),
        sa.Column('status', sa.Enum('open', 'accepted', 'rejected', 'closed', name='reportstatus'), nullable=False, server_default='open'),
        sa.Column('accepted_by', sa.BigInteger(), nullable=True),
        sa.Column('voice_channel_id', sa.BigInteger(), nullable=True),
        sa.Column('report_message_id', sa.BigInteger(), nullable=True),
        sa.Column('control_message_id', sa.BigInteger(), nullable=True),
        sa.Column('verdict', sa.String(length=32), nullable=True),
        sa.Column('verdict_comment', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.Column('closed_at', sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_reports_guild_id', 'reports', ['guild_id'])

    # report_moves
    op.create_table(
        'report_moves',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('report_id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('origin_channel_id', sa.BigInteger(), nullable=True),
        sa.ForeignKeyConstraint(['report_id'], ['reports.id'], ondelete='CASCADE'),
        sa.PrimaryKeyConstraint('id')
    )

    # audit_log
    op.create_table(
        'audit_log',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('action', sa.String(length=128), nullable=False),
        sa.Column('actor_id', sa.BigInteger(), nullable=True),
        sa.Column('target_id', sa.BigInteger(), nullable=True),
        sa.Column('punishment_id', sa.Integer(), nullable=True),
        sa.Column('report_id', sa.Integer(), nullable=True),
        sa.Column('details', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_audit_log_guild_id', 'audit_log', ['guild_id'])
    op.create_index('ix_audit_log_created_at', 'audit_log', ['created_at'])

    # panel_access
    op.create_table(
        'panel_access',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('role_id', sa.BigInteger(), nullable=False),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('guild_id', 'role_id', name='uq_panel_access')
    )
    op.create_index('ix_panel_access_guild_id', 'panel_access', ['guild_id'])

    # report_spam
    op.create_table(
        'report_spam',
        sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
        sa.Column('guild_id', sa.BigInteger(), nullable=False),
        sa.Column('user_id', sa.BigInteger(), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), server_default=sa.text('now()'), nullable=False),
        sa.PrimaryKeyConstraint('id')
    )
    op.create_index('ix_report_spam_user_time', 'report_spam', ['guild_id', 'user_id', 'created_at'])


def downgrade() -> None:
    op.drop_table('report_spam')
    op.drop_table('panel_access')
    op.drop_table('audit_log')
    op.drop_table('report_moves')
    op.drop_table('reports')
    op.drop_table('role_backups')
    op.drop_table('punishments')
    op.drop_table('users')
    op.drop_table('button_roles')
    op.drop_table('button_config')
    op.drop_table('guild_settings')

    op.execute('DROP TYPE IF EXISTS buttonstyle')
    op.execute('DROP TYPE IF EXISTS reportstatus')
    op.execute('DROP TYPE IF EXISTS punishmenttype')
