from datetime import datetime, timezone
from decimal import Decimal

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db


competition_teams = db.Table(
    "competition_teams",
    db.Column("competition_id", db.Integer, db.ForeignKey("competitions.id"), primary_key=True),
    db.Column("team_id", db.Integer, db.ForeignKey("teams.id"), primary_key=True),
)


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), unique=True)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    team = db.relationship("Team", back_populates="user", foreign_keys=[team_id])

    def set_password(self, password):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password):
        return check_password_hash(self.password_hash, password)

    @property
    def is_active_user(self):
        return self.is_active


class PlatformSetting(db.Model):
    __tablename__ = "platform_settings"

    id = db.Column(db.Integer, primary_key=True, default=1)
    scoreboard_competition_selector_enabled = db.Column(
        db.Boolean, nullable=False, default=False
    )


class Team(db.Model):
    __tablename__ = "teams"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), unique=True, nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    user = db.relationship("User", back_populates="team", uselist=False)
    competitions = db.relationship("Competition", secondary=competition_teams, back_populates="teams")
    check_configurations = db.relationship(
        "TeamServiceConfig", back_populates="team", cascade="all, delete-orphan"
    )


class Competition(db.Model):
    __tablename__ = "competitions"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    status = db.Column(db.String(20), nullable=False, default="draft", index=True)
    interval_seconds = db.Column(db.Integer, nullable=False, default=60)
    payout_per_point = db.Column(db.Numeric(12, 2), nullable=False, default=Decimal("10.00"))
    round_number = db.Column(db.Integer, nullable=False, default=0)
    next_round_at = db.Column(db.DateTime(timezone=True))
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))

    teams = db.relationship("Team", secondary=competition_teams, back_populates="competitions")
    hosts = db.relationship("Host", back_populates="competition", cascade="all, delete-orphan")
    ip_templates = db.relationship("IPTemplate", back_populates="competition", cascade="all, delete-orphan")
    host_groups = db.relationship("HostGroup", back_populates="competition", cascade="all, delete-orphan")
    services = db.relationship("Service", back_populates="competition", cascade="all, delete-orphan")
    rounds = db.relationship("ScoreRound", back_populates="competition", cascade="all, delete-orphan")


class Host(db.Model):
    __tablename__ = "hosts"
    __table_args__ = (
        db.UniqueConstraint(
            "competition_id",
            "team_id",
            "host_group_id",
            name="uq_host_competition_team_group",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(db.Integer, db.ForeignKey("competitions.id"), nullable=False)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False)
    host_group_id = db.Column(db.Integer, db.ForeignKey("host_groups.id"))
    address = db.Column(db.String(255), nullable=False)
    team_can_edit_address = db.Column(db.Boolean, nullable=False, default=False)
    allowed_address_ranges = db.Column(db.String(1000), nullable=False, default="")
    competition = db.relationship("Competition", back_populates="hosts")
    team = db.relationship("Team")
    host_group = db.relationship("HostGroup", back_populates="hosts")


class IPTemplate(db.Model):
    __tablename__ = "ip_templates"
    __table_args__ = (
        db.UniqueConstraint(
            "competition_id",
            "name",
            name="uq_ip_template_competition_name",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competitions.id"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    address_template = db.Column(db.String(253), nullable=False)
    index_start = db.Column(db.Integer, nullable=False)
    index_end = db.Column(db.Integer, nullable=False)
    competition = db.relationship("Competition", back_populates="ip_templates")
    host_groups = db.relationship("HostGroup", back_populates="ip_template")


class HostGroup(db.Model):
    __tablename__ = "host_groups"
    __table_args__ = (
        db.UniqueConstraint(
            "competition_id",
            "name",
            name="uq_host_group_competition_name",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competitions.id"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    ip_template_id = db.Column(
        db.Integer,
        db.ForeignKey("ip_templates.id"),
        nullable=True,
    )
    check_types = db.Column(db.JSON, nullable=False, default=list)
    competition = db.relationship("Competition", back_populates="host_groups")
    ip_template = db.relationship("IPTemplate", back_populates="host_groups")
    hosts = db.relationship("Host", back_populates="host_group", cascade="all, delete-orphan")
    services = db.relationship("Service", back_populates="host_group")


class ServiceGroup(db.Model):
    __tablename__ = "service_groups"
    __table_args__ = (
        db.UniqueConstraint(
            "competition_id",
            "name",
            name="uq_service_group_competition_name",
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(
        db.Integer,
        db.ForeignKey("competitions.id"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(120), nullable=False)
    display_name = db.Column(db.String(120), nullable=False)
    competition = db.relationship("Competition")
    services = db.relationship("Service", back_populates="service_group")


class Service(db.Model):
    __tablename__ = "services"

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(db.Integer, db.ForeignKey("competitions.id"), nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    check_type = db.Column(db.String(20), nullable=False, default="http")
    check_config = db.Column(db.JSON, nullable=False, default=dict)
    protocol = db.Column(db.String(10), nullable=False, default="http")
    port = db.Column(db.Integer, nullable=False)
    path = db.Column(db.String(255), nullable=False, default="/")
    expected_status = db.Column(db.Integer, nullable=False, default=200)
    expected_text = db.Column(db.String(255))
    points = db.Column(db.Integer, nullable=False, default=1)
    points_boost = db.Column(db.Integer, nullable=False, default=0)
    timeout_seconds = db.Column(db.Integer, nullable=False, default=3)
    team_editable_fields = db.Column(db.JSON, nullable=False, default=list)
    enabled = db.Column(db.Boolean, nullable=False, default=True)
    paused = db.Column(db.Boolean, nullable=False, default=False)
    hidden_from_scoreboard = db.Column(db.Boolean, nullable=False, default=False)
    service_group_id = db.Column(db.Integer, db.ForeignKey("service_groups.id"))
    host_group_id = db.Column(db.Integer, db.ForeignKey("host_groups.id"))
    competition = db.relationship("Competition", back_populates="services")
    service_group = db.relationship("ServiceGroup", back_populates="services")
    host_group = db.relationship("HostGroup", back_populates="services")
    team_configurations = db.relationship(
        "TeamServiceConfig", back_populates="service", cascade="all, delete-orphan"
    )


class TeamServiceConfig(db.Model):
    __tablename__ = "team_service_configs"
    __table_args__ = (
        db.UniqueConstraint(
            "team_id", "service_id", name="uq_team_service_config_team_service"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"), nullable=False, index=True)
    credentials_override = db.Column(db.Text)
    team = db.relationship("Team", back_populates="check_configurations")
    service = db.relationship("Service", back_populates="team_configurations")


class ScoreRound(db.Model):
    __tablename__ = "score_rounds"
    __table_args__ = (
        db.UniqueConstraint("competition_id", "number", name="uq_round_competition_number"),
    )

    id = db.Column(db.Integer, primary_key=True)
    competition_id = db.Column(db.Integer, db.ForeignKey("competitions.id"), nullable=False, index=True)
    number = db.Column(db.Integer, nullable=False)
    status = db.Column(db.String(20), nullable=False, default="queued")
    error_message = db.Column(db.String(500))
    started_at = db.Column(db.DateTime(timezone=True))
    completed_at = db.Column(db.DateTime(timezone=True))
    competition = db.relationship("Competition", back_populates="rounds")
    results = db.relationship("ScoreResult", back_populates="score_round", cascade="all, delete-orphan")


class ScoreResult(db.Model):
    __tablename__ = "score_results"
    __table_args__ = (
        db.UniqueConstraint("round_id", "team_id", "service_id", name="uq_result_round_team_service"),
    )

    id = db.Column(db.Integer, primary_key=True)
    round_id = db.Column(db.Integer, db.ForeignKey("score_rounds.id"), nullable=False, index=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    service_id = db.Column(db.Integer, db.ForeignKey("services.id"), nullable=False)
    passed = db.Column(db.Boolean, nullable=False)
    points = db.Column(db.Integer, nullable=False, default=0)
    latency_ms = db.Column(db.Integer)
    details = db.Column(db.String(500), nullable=False, default="")
    checked_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    score_round = db.relationship("ScoreRound", back_populates="results")
    team = db.relationship("Team")
    service = db.relationship("Service")


class BankTransaction(db.Model):
    __tablename__ = "bank_transactions"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    score_result_id = db.Column(db.Integer, db.ForeignKey("score_results.id"), unique=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), unique=True)
    kind = db.Column(db.String(10), nullable=False)
    amount = db.Column(db.Numeric(12, 2), nullable=False)
    description = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc), index=True)
    team = db.relationship("Team")
    created_by = db.relationship("User")


class Product(db.Model):
    __tablename__ = "products"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.Text, nullable=False, default="")
    price = db.Column(db.Numeric(12, 2), nullable=False)
    stock_count = db.Column(db.Integer)
    max_per_team = db.Column(db.Integer)
    available_until = db.Column(db.DateTime(timezone=True))
    published = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    questions = db.relationship("ProductQuestion", back_populates="product", cascade="all, delete-orphan")


class ProductQuestion(db.Model):
    __tablename__ = "product_questions"

    id = db.Column(db.Integer, primary_key=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    text = db.Column(db.String(255), nullable=False)
    product = db.relationship("Product", back_populates="questions")


class Order(db.Model):
    __tablename__ = "orders"

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(db.Integer, db.ForeignKey("teams.id"), nullable=False, index=True)
    product_id = db.Column(db.Integer, db.ForeignKey("products.id"), nullable=False)
    quantity = db.Column(db.Integer, nullable=False, default=1)
    unit_price = db.Column(db.Numeric(12, 2), nullable=False)
    status = db.Column(db.String(20), nullable=False, default="pending")
    created_at = db.Column(db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(timezone.utc))
    completed_at = db.Column(db.DateTime(timezone=True))
    completed_by_id = db.Column(db.Integer, db.ForeignKey("users.id"))
    team = db.relationship("Team")
    product = db.relationship("Product")
    completed_by = db.relationship("User")
    answers = db.relationship("OrderAnswer", back_populates="order", cascade="all, delete-orphan")

    @property
    def total(self):
        return self.unit_price * self.quantity


class OrderAnswer(db.Model):
    __tablename__ = "order_answers"

    id = db.Column(db.Integer, primary_key=True)
    order_id = db.Column(db.Integer, db.ForeignKey("orders.id"), nullable=False)
    question_id = db.Column(db.Integer, db.ForeignKey("product_questions.id"), nullable=False)
    answer = db.Column(db.String(500), nullable=False)
    question = db.relationship("ProductQuestion")
    order = db.relationship("Order", back_populates="answers")
