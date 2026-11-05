"""The database schema exactly as it stood at revision 20261105_17. Frozen: never edit.

A fresh database used to be built from the current models, so what a migration
produced depended on when you ran it. Fresh databases now start here, and every
later change is an ordinary migration, so the same steps always give the same schema.
"""
import sqlalchemy as sa

metadata = sa.MetaData()

sa.Table(
    "organizations", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
)

sa.Table(
    "materials", metadata,
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('unit', sa.String(length=20), nullable=False),
    sa.UniqueConstraint('organization_id', 'name', name='uq_material_org_name'),
)

sa.Table(
    "projects", metadata,
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('code', sa.String(length=50), nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('job_location', sa.String(length=200), nullable=True),
    sa.Column('description', sa.Text(), nullable=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint('organization_id', 'code', name='uq_project_org_code'),
)

sa.Table(
    "suppliers", metadata,
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('name', sa.String(length=200), nullable=False),
    sa.Column('contact', sa.String(length=300), nullable=True),
    sa.UniqueConstraint('organization_id', 'name', name='uq_supplier_org_name'),
)

sa.Table(
    "users", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('username', sa.String(length=100), nullable=False, unique=True),
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('password_hash', sa.String(length=255), nullable=False),
    sa.Column('active', sa.Boolean(), nullable=False),
    sa.Column('role', sa.String(length=20), nullable=False, server_default='owner'),
    sa.Column('display_name', sa.String(length=100), nullable=True),
    sa.Column('must_change_password', sa.Boolean(), nullable=False, server_default=sa.false()),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=True),
    sa.UniqueConstraint('username', name=None),
)

sa.Table(
    "account_events", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('target_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('old_value', sa.String(length=200), nullable=True),
    sa.Column('new_value', sa.String(length=200), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    "batches", metadata,
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=True, index=True),
    sa.Column('layout', sa.String(length=20), nullable=False),
    sa.Column('intake_mode', sa.String(length=20), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('expected_ticket_count', sa.Integer(), nullable=True),
)

sa.Table(
    "invoice_comparisons", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=False, index=True),
    sa.Column('invoice_number', sa.String(length=100), nullable=False),
    sa.Column('supplier_name', sa.String(length=200), nullable=False),
    sa.Column('supplier_key', sa.String(length=200), nullable=False),
    sa.Column('material_name', sa.String(length=200), nullable=False),
    sa.Column('material_key', sa.String(length=200), nullable=False),
    sa.Column('date_from', sa.Date(), nullable=False),
    sa.Column('date_to', sa.Date(), nullable=False),
    sa.Column('billed_tons', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('billed_unit_price', sa.Numeric(precision=12, scale=4), nullable=False),
    sa.Column('billed_subtotal', sa.Numeric(precision=14, scale=2), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
)

sa.Table(
    "invoice_documents", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=True),
    sa.Column('original_name', sa.String(length=255), nullable=False),
    sa.Column('storage_path', sa.String(length=500), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False, index=True),
    sa.Column('page_count', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('extractor', sa.String(length=30), nullable=False),
    sa.Column('message', sa.Text(), nullable=True),
    sa.Column('unread_rows', sa.JSON(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    "login_sessions", metadata,
    sa.Column('token_digest', sa.String(length=64), primary_key=True, nullable=False),
    sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False, index=True),
    sa.Column('expires_at', sa.DateTime(), nullable=False, index=True),
)

sa.Table(
    "name_aliases", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('kind', sa.String(length=20), nullable=False),
    sa.Column('alias_text', sa.String(length=200), nullable=False),
    sa.Column('alias_key', sa.String(length=200), nullable=False),
    sa.Column('target_name', sa.String(length=200), nullable=False),
    sa.Column('target_key', sa.String(length=200), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('removed_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('removed_at', sa.DateTime(timezone=True), nullable=True),
    sa.Index('uq_name_alias_active', 'organization_id', 'kind', 'alias_key', unique=True, sqlite_where=sa.text('removed_at IS NULL'), postgresql_where=sa.text('removed_at IS NULL')),
)

sa.Table(
    "pricing_events", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('supplier_name', sa.String(length=200), nullable=True),
    sa.Column('material_name', sa.String(length=200), nullable=True),
    sa.Column('old_value', sa.String(length=40), nullable=True),
    sa.Column('new_value', sa.String(length=40), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    "saved_views", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('filters', sa.JSON(), nullable=False),
    sa.Column('columns', sa.JSON(), nullable=False),
    sa.Column('created_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.UniqueConstraint('organization_id', 'name', name='uq_saved_view_org_name'),
)

sa.Table(
    "supplier_rates", metadata,
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('supplier_id', sa.Integer(), sa.ForeignKey('suppliers.id', ondelete='CASCADE'), nullable=False),
    sa.Column('material_id', sa.Integer(), sa.ForeignKey('materials.id', ondelete='CASCADE'), nullable=False),
    sa.Column('unit_price', sa.Numeric(precision=12, scale=4), nullable=False),
)

sa.Table(
    "invoice_comparison_events", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('comparison_id', sa.Integer(), sa.ForeignKey('invoice_comparisons.id'), nullable=False, index=True),
    sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('field', sa.String(length=60), nullable=False),
    sa.Column('old_value', sa.Text(), nullable=True),
    sa.Column('new_value', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    "invoice_draft_lines", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('document_id', sa.Integer(), sa.ForeignKey('invoice_documents.id'), nullable=False, index=True),
    sa.Column('page', sa.Integer(), nullable=False),
    sa.Column('source_text', sa.Text(), nullable=True),
    sa.Column('state', sa.String(length=20), nullable=False),
    sa.Column('project_id', sa.Integer(), sa.ForeignKey('projects.id'), nullable=True),
    sa.Column('invoice_number', sa.String(length=100), nullable=True),
    sa.Column('supplier_name', sa.String(length=200), nullable=True),
    sa.Column('material_name', sa.String(length=200), nullable=True),
    sa.Column('billed_quantity', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('unit', sa.String(length=20), nullable=True),
    sa.Column('billed_unit_price', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('billed_subtotal', sa.Numeric(precision=14, scale=2), nullable=True),
    sa.Column('date_from', sa.Date(), nullable=True),
    sa.Column('date_to', sa.Date(), nullable=True),
    sa.Column('ticket_list', sa.Text(), nullable=False),
    sa.Column('comparison_id', sa.Integer(), sa.ForeignKey('invoice_comparisons.id'), nullable=True, index=True),
    sa.Column('reviewed_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('reviewed_at', sa.DateTime(timezone=True), nullable=True),
)

sa.Table(
    "invoice_ticket_refs", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('comparison_id', sa.Integer(), sa.ForeignKey('invoice_comparisons.id'), nullable=False, index=True),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('ticket_number', sa.String(length=100), nullable=False),
    sa.Column('ticket_key', sa.String(length=100), nullable=False, index=True),
    sa.Column('billed_tons', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.UniqueConstraint('comparison_id', 'position', name='uq_invoice_ticket_ref_position'),
)

sa.Table(
    "processing_jobs", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('batch_id', sa.Integer(), sa.ForeignKey('batches.id'), nullable=False, index=True),
    sa.Column('status', sa.String(length=20), nullable=False, index=True),
    sa.Column('only_failed', sa.Boolean(), nullable=False),
    sa.Column('attempts', sa.Integer(), nullable=False),
    sa.Column('claim_token', sa.String(length=64), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    "review_timings", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('organization_id', sa.Integer(), sa.ForeignKey('organizations.id'), nullable=False, index=True),
    sa.Column('batch_id', sa.Integer(), sa.ForeignKey('batches.id'), nullable=False),
    sa.Column('intake_mode', sa.String(length=20), nullable=False),
    sa.Column('seconds', sa.Numeric(precision=8, scale=1), nullable=False),
    sa.Column('fields_corrected', sa.Integer(), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    "uploaded_files", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('batch_id', sa.Integer(), sa.ForeignKey('batches.id'), nullable=False),
    sa.Column('original_name', sa.String(length=255), nullable=False),
    sa.Column('storage_path', sa.String(length=500), nullable=False),
    sa.Column('sha256', sa.String(length=64), nullable=False, index=True),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Index('ix_uploaded_files_batch_id', 'batch_id'),
)

sa.Table(
    "invoice_document_events", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('document_id', sa.Integer(), sa.ForeignKey('invoice_documents.id'), nullable=False, index=True),
    sa.Column('line_id', sa.Integer(), sa.ForeignKey('invoice_draft_lines.id'), nullable=True),
    sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('old_value', sa.Text(), nullable=True),
    sa.Column('new_value', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    "scan_slots", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('batch_id', sa.Integer(), sa.ForeignKey('batches.id'), nullable=False, index=True),
    sa.Column('file_id', sa.Integer(), sa.ForeignKey('uploaded_files.id'), nullable=False, index=True),
    sa.Column('page', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('crop_box', sa.JSON(), nullable=False),
    sa.Column('crop_path', sa.String(length=500), nullable=False),
    sa.Column('crop_sha256', sa.String(length=64), nullable=False),
    sa.Column('ink_fraction', sa.Float(), nullable=False),
    sa.Column('detection', sa.String(length=20), nullable=False),
    sa.Column('decision', sa.String(length=20), nullable=True),
    sa.Column('decided_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('decided_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('rotation', sa.Integer(), nullable=False),
    sa.UniqueConstraint('file_id', 'page', 'position', name='uq_scan_slot_position'),
)

sa.Table(
    "tickets", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('batch_id', sa.Integer(), sa.ForeignKey('batches.id'), nullable=False, index=True),
    sa.Column('file_id', sa.Integer(), sa.ForeignKey('uploaded_files.id'), nullable=False),
    sa.Column('source_page', sa.Integer(), nullable=False),
    sa.Column('position', sa.Integer(), nullable=False),
    sa.Column('crop_box', sa.JSON(), nullable=False),
    sa.Column('crop_path', sa.String(length=500), nullable=False),
    sa.Column('crop_sha256', sa.String(length=64), nullable=False, index=True),
    sa.Column('ticket_number', sa.String(length=100), nullable=True),
    sa.Column('delivery_date', sa.Date(), nullable=True),
    sa.Column('material_type', sa.String(length=200), nullable=True),
    sa.Column('tonnage', sa.Numeric(precision=12, scale=3), nullable=True),
    sa.Column('supplier', sa.String(length=200), nullable=True),
    sa.Column('job_location', sa.String(length=200), nullable=True),
    sa.Column('unit_price_override', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('price_snapshot_state', sa.String(length=20), nullable=True),
    sa.Column('applied_unit_price', sa.Numeric(precision=12, scale=4), nullable=True),
    sa.Column('applied_price_source', sa.String(length=20), nullable=True),
    sa.Column('raw_extraction', sa.JSON(), nullable=True),
    sa.Column('flags', sa.JSON(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('tonnage_confirmed', sa.Boolean(), nullable=False),
    sa.Column('verified_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('error', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('flag_note', sa.String(length=300), nullable=True),
    sa.Index('ix_tickets_batch_status', 'batch_id', 'status'),
    sa.Index('ix_tickets_delivery_date', 'delivery_date'),
    sa.Index('ix_tickets_ticket_number', 'ticket_number'),
)

sa.Table(
    "audit_events", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('tickets.id'), nullable=False, index=True),
    sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('field', sa.String(length=50), nullable=False),
    sa.Column('old_value', sa.Text(), nullable=True),
    sa.Column('new_value', sa.Text(), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)

sa.Table(
    "invoice_ticket_links", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('comparison_id', sa.Integer(), sa.ForeignKey('invoice_comparisons.id'), nullable=False, index=True),
    sa.Column('ticket_id', sa.Integer(), sa.ForeignKey('tickets.id'), nullable=False),
    sa.Column('listed_number', sa.String(length=100), nullable=False),
    sa.Column('verified_tons', sa.Numeric(precision=12, scale=3), nullable=False),
    sa.Column('ticket_verified_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('confirmed_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('confirmed_at', sa.DateTime(timezone=True), nullable=False),
    sa.Column('released_by_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('released_at', sa.DateTime(timezone=True), nullable=True),
    sa.Column('release_reason', sa.String(length=200), nullable=True),
    sa.Index('uq_invoice_ticket_link_active', 'ticket_id', unique=True, sqlite_where=sa.text('released_at IS NULL'), postgresql_where=sa.text('released_at IS NULL')),
)

sa.Table(
    "scan_events", metadata,
    sa.Column('id', sa.Integer(), primary_key=True, nullable=False),
    sa.Column('batch_id', sa.Integer(), sa.ForeignKey('batches.id'), nullable=False, index=True),
    sa.Column('slot_id', sa.Integer(), sa.ForeignKey('scan_slots.id'), nullable=True, index=True),
    sa.Column('actor_user_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=True),
    sa.Column('action', sa.String(length=40), nullable=False),
    sa.Column('old_value', sa.String(length=100), nullable=True),
    sa.Column('new_value', sa.String(length=100), nullable=True),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
)
