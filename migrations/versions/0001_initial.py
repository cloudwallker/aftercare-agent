# ruff: noqa: E501
"""初始八张业务表。DDL 固定于本版本，不依赖运行时 ORM。"""

from alembic import op

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    op.execute("""
CREATE SCHEMA app
    """)
    op.execute("""
CREATE SCHEMA merchant
    """)
    op.execute("""
CREATE TABLE app.command_requests (
	id UUID NOT NULL,
	actor_id TEXT NOT NULL,
	route_scope TEXT NOT NULL,
	idempotency_key TEXT NOT NULL,
	request_hash CHAR(64) NOT NULL,
	response_status INTEGER,
	response_json JSONB,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_commands_key UNIQUE (actor_id, route_scope, idempotency_key)
)
    """)
    op.execute("""
CREATE TABLE app.tasks (
	id UUID NOT NULL,
	customer_id TEXT NOT NULL,
	order_id TEXT NOT NULL,
	message TEXT NOT NULL,
	status TEXT DEFAULT 'queued' NOT NULL,
	phase TEXT DEFAULT 'investigate' NOT NULL,
	generation INTEGER DEFAULT '1' NOT NULL,
	checkpoint JSONB DEFAULT '{"schema_version": 1,"generation": 1,"messages":[],"next_step_no": 1,"evidence_by_tool":{},"invalid_output_count": 0}'::jsonb NOT NULL,
	model_call_count INTEGER DEFAULT '0' NOT NULL,
	tool_call_count INTEGER DEFAULT '0' NOT NULL,
	generation_started_at TIMESTAMP WITH TIME ZONE,
	next_run_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	lease_owner TEXT,
	lease_epoch BIGINT DEFAULT '0' NOT NULL,
	lease_expires_at TIMESTAMP WITH TIME ZONE,
	last_event_seq BIGINT DEFAULT '0' NOT NULL,
	result JSONB,
	error_code TEXT,
	error_detail TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_tasks_status CHECK (status IN ('queued','running','waiting_approval','succeeded','rejected','failed','manual_review')),
	CONSTRAINT ck_tasks_phase CHECK (phase IN ('investigate','execute_refund')),
	CONSTRAINT ck_tasks_generation CHECK (generation BETWEEN 1 AND 3),
	CONSTRAINT ck_tasks_counters CHECK (model_call_count >= 0 AND tool_call_count >= 0 AND lease_epoch >= 0 AND last_event_seq >= 0),
	CONSTRAINT ck_tasks_lease CHECK ((status = 'running' AND lease_owner IS NOT NULL AND lease_expires_at IS NOT NULL) OR (status <> 'running' AND lease_owner IS NULL AND lease_expires_at IS NULL))
)
    """)
    op.execute("""
CREATE INDEX ix_tasks_customer ON app.tasks (customer_id, created_at, id)
    """)
    op.execute("""
CREATE INDEX ix_tasks_lease ON app.tasks (status, lease_expires_at)
    """)
    op.execute("""
CREATE INDEX ix_tasks_queue ON app.tasks (status, next_run_at, created_at)
    """)
    op.execute("""
CREATE TABLE app.approvals (
	id UUID NOT NULL,
	task_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	status TEXT DEFAULT 'pending' NOT NULL,
	payload JSONB NOT NULL,
	payload_hash CHAR(64) NOT NULL,
	summary TEXT NOT NULL,
	expires_at TIMESTAMP WITH TIME ZONE NOT NULL,
	decision_key TEXT,
	decision_hash CHAR(64),
	decided_by TEXT,
	decided_at TIMESTAMP WITH TIME ZONE,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT uq_approvals_generation UNIQUE (task_id, generation),
	CONSTRAINT ck_approvals_generation CHECK (generation BETWEEN 1 AND 3),
	CONSTRAINT ck_approvals_status CHECK (status IN ('pending','approved','rejected','expired','invalidated')),
	FOREIGN KEY(task_id) REFERENCES app.tasks (id)
)
    """)
    op.execute("""
CREATE TABLE app.events (
	task_id UUID NOT NULL,
	seq BIGINT NOT NULL,
	type TEXT NOT NULL,
	data JSONB NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	PRIMARY KEY (task_id, seq),
	CONSTRAINT ck_events_seq CHECK (seq > 0),
	FOREIGN KEY(task_id) REFERENCES app.tasks (id)
)
    """)
    op.execute("""
CREATE TABLE app.steps (
	id UUID NOT NULL,
	task_id UUID NOT NULL,
	generation INTEGER NOT NULL,
	step_no INTEGER NOT NULL,
	kind TEXT NOT NULL,
	name TEXT NOT NULL,
	status TEXT DEFAULT 'pending' NOT NULL,
	attempt_count INTEGER DEFAULT '0' NOT NULL,
	input JSONB NOT NULL,
	output JSONB,
	error_code TEXT,
	started_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (id),
	CONSTRAINT uq_steps_position UNIQUE (task_id, generation, step_no),
	CONSTRAINT ck_steps_counters CHECK (generation BETWEEN 1 AND 3 AND step_no > 0 AND attempt_count >= 0),
	CONSTRAINT ck_steps_kind CHECK (kind IN ('model','tool')),
	CONSTRAINT ck_steps_status CHECK (status IN ('pending','succeeded','failed')),
	FOREIGN KEY(task_id) REFERENCES app.tasks (id)
)
    """)
    op.execute("""
CREATE TABLE app.refund_operations (
	id UUID NOT NULL,
	task_id UUID NOT NULL,
	approval_id UUID NOT NULL,
	order_id TEXT NOT NULL,
	generation INTEGER NOT NULL,
	operation_key TEXT NOT NULL,
	request JSONB NOT NULL,
	request_hash CHAR(64) NOT NULL,
	status TEXT DEFAULT 'prepared' NOT NULL,
	dispatch_count INTEGER DEFAULT '0' NOT NULL,
	reconcile_count INTEGER DEFAULT '0' NOT NULL,
	reconcile_deadline TIMESTAMP WITH TIME ZONE NOT NULL,
	merchant_refund_id UUID,
	merchant_result JSONB,
	last_error_code TEXT,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_operations_status CHECK (status IN ('prepared','unknown','confirmed','declined')),
	CONSTRAINT ck_operations_counters CHECK (generation BETWEEN 1 AND 3 AND dispatch_count >= 0 AND reconcile_count >= 0),
	UNIQUE (task_id),
	FOREIGN KEY(task_id) REFERENCES app.tasks (id),
	UNIQUE (approval_id),
	FOREIGN KEY(approval_id) REFERENCES app.approvals (id),
	UNIQUE (operation_key)
)
    """)
    op.execute("""
CREATE UNIQUE INDEX uq_operations_reserved_order ON app.refund_operations (order_id) WHERE status IN ('prepared','unknown','confirmed')
    """)
    op.execute("""
CREATE TABLE merchant.orders (
	id TEXT NOT NULL,
	customer_id TEXT NOT NULL,
	paid_amount_cents BIGINT NOT NULL,
	currency TEXT NOT NULL,
	payment_status TEXT NOT NULL,
	shipment_status TEXT NOT NULL,
	delay_days INTEGER DEFAULT '0' NOT NULL,
	refunded BOOLEAN DEFAULT false NOT NULL,
	order_version BIGINT DEFAULT '1' NOT NULL,
	updated_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	PRIMARY KEY (id),
	CONSTRAINT ck_orders_values CHECK (paid_amount_cents > 0 AND delay_days >= 0 AND order_version > 0)
)
    """)
    op.execute("""
CREATE TABLE merchant.refund_requests (
	operation_key TEXT NOT NULL,
	request_hash CHAR(64) NOT NULL,
	request JSONB NOT NULL,
	order_id TEXT NOT NULL,
	status TEXT NOT NULL,
	refund_id UUID,
	response JSONB,
	response_http_status INTEGER,
	fault_consumed BOOLEAN DEFAULT false NOT NULL,
	created_at TIMESTAMP WITH TIME ZONE DEFAULT clock_timestamp() NOT NULL,
	completed_at TIMESTAMP WITH TIME ZONE,
	PRIMARY KEY (operation_key),
	CONSTRAINT ck_refund_requests_status CHECK (status IN ('processing','confirmed','declined')),
	UNIQUE (refund_id)
)
    """)
    op.execute("""
CREATE UNIQUE INDEX uq_refund_requests_confirmed_order ON merchant.refund_requests (order_id) WHERE status = 'confirmed'
    """)
    op.execute("""
CREATE FUNCTION app.guard_approval() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.id,NEW.task_id,NEW.generation,NEW.payload,NEW.payload_hash,NEW.summary,NEW.expires_at,NEW.created_at)
 IS DISTINCT FROM ROW(OLD.id,OLD.task_id,OLD.generation,OLD.payload,OLD.payload_hash,OLD.summary,OLD.expires_at,OLD.created_at) THEN
 RAISE EXCEPTION 'approval payload is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$
    """)
    op.execute("""
CREATE TRIGGER approval_immutable BEFORE UPDATE ON app.approvals FOR EACH ROW EXECUTE FUNCTION app.guard_approval()
    """)
    op.execute("""
CREATE FUNCTION app.guard_operation() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.id,NEW.task_id,NEW.approval_id,NEW.order_id,NEW.generation,NEW.operation_key,NEW.request,NEW.request_hash,NEW.reconcile_deadline,NEW.created_at)
 IS DISTINCT FROM ROW(OLD.id,OLD.task_id,OLD.approval_id,OLD.order_id,OLD.generation,OLD.operation_key,OLD.request,OLD.request_hash,OLD.reconcile_deadline,OLD.created_at) THEN
 RAISE EXCEPTION 'refund operation request is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$
    """)
    op.execute("""
CREATE TRIGGER operation_immutable BEFORE UPDATE ON app.refund_operations FOR EACH ROW EXECUTE FUNCTION app.guard_operation()
    """)
    op.execute("""
CREATE FUNCTION merchant.guard_refund_request() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
 IF ROW(NEW.operation_key,NEW.request_hash,NEW.request,NEW.order_id,NEW.created_at)
 IS DISTINCT FROM ROW(OLD.operation_key,OLD.request_hash,OLD.request,OLD.order_id,OLD.created_at) THEN
 RAISE EXCEPTION 'merchant refund request is immutable' USING ERRCODE='23514';
 END IF;
 RETURN NEW;
END $$
    """)
    op.execute("""
CREATE TRIGGER refund_request_immutable BEFORE UPDATE ON merchant.refund_requests FOR EACH ROW EXECUTE FUNCTION merchant.guard_refund_request()
    """)
    op.execute("""
REVOKE ALL ON SCHEMA app, merchant FROM PUBLIC
    """)
    op.execute("""
REVOKE ALL ON ALL TABLES IN SCHEMA app, merchant FROM PUBLIC
    """)
    op.execute("""
GRANT USAGE ON SCHEMA app TO aftercare_app
    """)
    op.execute("""
GRANT USAGE ON SCHEMA merchant TO aftercare_merchant
    """)
    op.execute("""
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA app TO aftercare_app
    """)
    op.execute("""
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA merchant TO aftercare_merchant
    """)
    op.execute("""
REVOKE ALL ON public.alembic_version FROM PUBLIC
    """)
    op.execute("""
GRANT SELECT ON public.alembic_version TO aftercare_app, aftercare_merchant
    """)


def downgrade():
    op.execute("REVOKE SELECT ON public.alembic_version FROM aftercare_app, aftercare_merchant")
    op.execute("DROP SCHEMA merchant CASCADE")
    op.execute("DROP SCHEMA app CASCADE")
