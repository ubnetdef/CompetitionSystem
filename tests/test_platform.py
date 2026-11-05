import socket
import unittest
import base64
import hashlib
import json
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from decimal import Decimal
import requests
from unittest.mock import patch

from app import create_app
from app.bank import balance_for
from app.check_catalog import CHECK_TYPE_PAYOUTS
from app.extensions import db
from app.check_secrets import decrypt_credentials, encrypt_credentials
from app.main import _recommended_score_check_action
from cryptography.fernet import Fernet
from app.models import (
    BankTransaction,
    Competition,
    Host,
    HostGroup,
    IPTemplate,
    PlatformSetting,
    Order,
    OrderAnswer,
    Product,
    ProductQuestion,
    ScoreRound,
    ScoreResult,
    Service,
    ServiceGroup,
    Team,
    TeamServiceConfig,
    User,
)
from app.scoring import _credentials, _probe, process_scoring_round
from app.tasks import (
    claim_next_round,
    recover_stale_rounds,
    run_scoring_round,
    schedule_due_rounds,
)


class PlatformTests(unittest.TestCase):
    def setUp(self):
        self.app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "test-key",
                "CHECK_CREDENTIAL_KEY": "1" * 64,
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
                "SESSION_COOKIE_DOMAIN": None,
            }
        )
        self.context = self.app.app_context()
        self.context.push()
        db.create_all()

        self.team = Team(name="Blue Team")
        db.session.add(self.team)
        db.session.flush()
        self.team_user = User(username="blue", role="team", team=self.team)
        self.team_user.set_password("a-long-test-password")
        self.admin = User(username="admin", role="admin")
        self.admin.set_password("another-long-test-password")
        db.session.add_all([self.team_user, self.admin])
        db.session.commit()
        self.client = self.app.test_client()

    def tearDown(self):
        db.session.remove()
        db.drop_all()
        self.context.pop()

    def login(self, username, password):
        return self.client.post(
            "/login", data={"username": username, "password": password}
        )

    def test_deployment_health_and_readiness_commands(self):
        response = self.client.get("/healthz")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json, {"status": "ok"})

        runner = self.app.test_cli_runner()
        database_check = runner.invoke(args=["db-check"])
        self.assertEqual(database_check.exit_code, 0, database_check.output)
        admin_check = runner.invoke(args=["has-admin"])
        self.assertEqual(admin_check.exit_code, 0, admin_check.output)

        db.session.delete(self.admin)
        db.session.commit()
        missing_admin_check = runner.invoke(args=["has-admin"])
        self.assertNotEqual(missing_admin_check.exit_code, 0)

    def test_shared_login_and_team_bank_api(self):
        response = self.login("blue", "a-long-test-password")
        self.assertEqual(response.status_code, 302)
        response = self.client.get("/api/v1/me")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["team"]["name"], "Blue Team")
        response = self.client.get("/api/v1/me/bank")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["balance"], "0")
        self.assertEqual(response.json["currency"], "USD")

    def test_account_directory_never_exposes_password_hashes(self):
        self.login("admin", "another-long-test-password")

        response = self.client.get("/admin/users")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Password Hash", response.data)
        self.assertNotIn(self.admin.password_hash.encode(), response.data)

    def test_admin_can_rename_accounts_and_reject_duplicate_usernames(self):
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/users/{self.team_user.id}/username",
            data={"username": "blue-renamed"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            db.session.get(User, self.team_user.id).username,
            "blue-renamed",
        )

        response = self.client.post(
            f"/admin/users/{self.team_user.id}/username",
            data={"username": "ADMIN"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(
            db.session.get(User, self.team_user.id).username,
            "blue-renamed",
        )

    def test_removing_team_account_deletes_linked_team_records(self):
        competition = Competition(name="Team removal board", status="active")
        group = HostGroup(competition=competition, name="Linux")
        service = Service(
            competition=competition,
            host_group=group,
            name="Linux PING",
            check_type="ping",
            protocol="ping",
            port=0,
        )
        competition.teams.append(self.team)
        host = Host(
            competition=competition,
            team=self.team,
            host_group=group,
            address="192.0.2.10",
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="completed",
        )
        product = Product(
            name="Team removal item",
            price=Decimal("5.00"),
            published=True,
        )
        question = ProductQuestion(product=product, text="Size")
        order = Order(
            team=self.team,
            product=product,
            quantity=1,
            unit_price=Decimal("5.00"),
            status="pending",
        )
        order.answers.append(OrderAnswer(question=question, answer="M"))
        other_team = Team(name="Preserved team")
        db.session.add_all(
            [competition, group, service, host, score_round, product, other_team]
        )
        db.session.flush()
        result = ScoreResult(
            score_round=score_round,
            team=self.team,
            service=service,
            passed=True,
            points=1,
        )
        db.session.add_all(
            [
                result,
                order,
                TeamServiceConfig(
                    team=self.team,
                    service=service,
                    credentials_override="encrypted-test-value",
                ),
            ]
        )
        db.session.flush()
        db.session.add_all(
            [
                BankTransaction(
                    team=self.team,
                    kind="credit",
                    amount=Decimal("1.00"),
                    description="Score payout",
                    score_result_id=result.id,
                ),
                BankTransaction(
                    team=self.team,
                    kind="debit",
                    amount=Decimal("5.00"),
                    description="Store order",
                    order_id=order.id,
                ),
                BankTransaction(
                    team=other_team,
                    created_by=self.team_user,
                    kind="credit",
                    amount=Decimal("2.00"),
                    description="Unrelated transaction",
                ),
            ]
        )
        db.session.commit()
        team_id = self.team.id
        user_id = self.team_user.id
        other_team_id = other_team.id

        self.login("admin", "another-long-test-password")
        response = self.client.post(f"/admin/users/{user_id}/delete")

        self.assertEqual(response.status_code, 302)
        self.assertIsNone(db.session.get(User, user_id))
        self.assertIsNone(db.session.get(Team, team_id))
        self.assertEqual(Host.query.filter_by(team_id=team_id).count(), 0)
        self.assertEqual(TeamServiceConfig.query.filter_by(team_id=team_id).count(), 0)
        self.assertEqual(ScoreResult.query.filter_by(team_id=team_id).count(), 0)
        self.assertEqual(BankTransaction.query.filter_by(team_id=team_id).count(), 0)
        self.assertEqual(Order.query.filter_by(team_id=team_id).count(), 0)
        self.assertEqual(OrderAnswer.query.count(), 0)
        remaining_transaction = BankTransaction.query.filter_by(
            team_id=other_team_id
        ).one()
        self.assertIsNone(remaining_transaction.created_by_id)

    def test_account_removal_cannot_remove_the_signed_in_admin(self):
        self.login("admin", "another-long-test-password")

        response = self.client.post(f"/admin/users/{self.admin.id}/delete")

        self.assertEqual(response.status_code, 302)
        self.assertIsNotNone(db.session.get(User, self.admin.id))
        self.assertEqual(User.query.filter_by(role="admin").count(), 1)

    def test_login_explains_team_access_and_uses_ubnetdef_brand(self):
        response = self.client.get("/login")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"static/ubnetdef-logo.png", response.data)
        self.assertIn(b"Team tools", response.data)
        self.assertNotIn(b"YOUR TEAM WORKSPACE", response.data)
        self.assertNotIn(b"One login", response.data)
        self.assertIn(b"Live scoreboard", response.data)
        self.assertIn(b"Team bank", response.data)
        self.assertIn(b"Competition store", response.data)
        self.assertIn(b"Team settings", response.data)
        self.assertNotIn(b"Competition System", response.data)

    def test_admin_creates_team_account_from_display_name(self):
        old_team = Team(name="Old unassigned team")
        db.session.add(old_team)
        db.session.commit()
        self.login("admin", "another-long-test-password")
        page = self.client.get("/admin/users")
        self.assertNotIn(b"Existing team", page.data)
        self.assertIn(b'name="display_name"', page.data)

        response = self.client.post(
            "/admin/users",
            data={
                "username": "existing-team-login",
                "password": "secure-team-password",
                "role": "team",
                "display_name": "New scoreboard team",
                "team_id": str(old_team.id),
            },
        )

        self.assertEqual(response.status_code, 302)
        user = User.query.filter_by(username="existing-team-login").one()
        self.assertEqual(user.team.name, "New scoreboard team")
        self.assertIsNone(old_team.user)

    def test_account_directory_can_sort_by_username(self):
        zulu = User(username="zulu", role="banker")
        zulu.set_password("zulu-test-password")
        db.session.add(zulu)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get("/admin/users?sort=username&direction=asc")

        self.assertEqual(response.status_code, 200)
        self.assertLess(response.data.index(b'value="admin"'), response.data.index(b'value="blue"'))
        self.assertLess(response.data.index(b'value="blue"'), response.data.index(b'value="zulu"'))
        self.assertIn(b"Username", response.data)

    def test_bootstrap_cli_creates_one_admin_once(self):
        fresh_app = create_app(
            {
                "TESTING": True,
                "SECRET_KEY": "bootstrap-test-key",
                "CHECK_CREDENTIAL_KEY": "2" * 64,
                "SQLALCHEMY_DATABASE_URI": "sqlite://",
                "WTF_CSRF_ENABLED": False,
            }
        )
        with fresh_app.app_context():
            db.create_all()
            runner = fresh_app.test_cli_runner()
            result = runner.invoke(
                args=[
                    "bootstrap-admin",
                    "--username",
                    "first-admin",
                    "--password",
                    "a-secure-bootstrap-password",
                ]
            )
            self.assertEqual(result.exit_code, 0, result.output)
            admin = User.query.filter_by(username="first-admin").one()
            self.assertEqual(admin.role, "admin")

            result = runner.invoke(
                args=[
                    "bootstrap-admin",
                    "--username",
                    "second-admin",
                    "--password",
                    "another-secure-password",
                ]
            )
            self.assertNotEqual(result.exit_code, 0)
            self.assertEqual(User.query.filter_by(role="admin").count(), 1)
            db.drop_all()

    def test_bank_operator_adjustment_is_audited(self):
        self.login("admin", "another-long-test-password")
        response = self.client.post(
            "/bank/adjust",
            data={
                "team_id": self.team.id,
                "kind": "credit",
                "amount": "25",
                "description": "Competition award",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(balance_for(self.team.id), Decimal("25"))
        entry = BankTransaction.query.one()
        self.assertEqual(entry.created_by_id, self.admin.id)

    def test_bank_rejects_fractional_dollar_adjustments(self):
        self.login("admin", "another-long-test-password")
        response = self.client.post(
            "/bank/adjust",
            data={
                "team_id": self.team.id,
                "kind": "credit",
                "amount": "25.50",
                "description": "Fractional amount should fail",
            },
            follow_redirects=True,
        )
        self.assertIn(b"whole-dollar amount", response.data)
        self.assertEqual(BankTransaction.query.count(), 0)

    def test_banker_can_configure_whole_dollar_check_type_rewards(self):
        banker = User(username="payout-banker", role="banker")
        banker.set_password("banker-long-test-password")
        db.session.add(banker)
        db.session.commit()
        self.login("payout-banker", "banker-long-test-password")

        payload = {
            f"reward_{check_type}": "0"
            for check_type in CHECK_TYPE_PAYOUTS
        }
        payload["reward_http"] = "25"
        response = self.client.post("/bank/check-payouts", data=payload)

        self.assertEqual(response.status_code, 302)
        settings = db.session.get(PlatformSetting, 1)
        self.assertEqual(settings.check_type_payouts["http"], 25)
        self.assertEqual(
            set(settings.check_type_payouts),
            set(CHECK_TYPE_PAYOUTS),
        )

        payload["reward_http"] = "25.50"
        response = self.client.post(
            "/bank/check-payouts",
            data=payload,
            follow_redirects=True,
        )
        self.assertIn(b"whole-dollar amount", response.data)
        db.session.refresh(settings)
        self.assertEqual(settings.check_type_payouts["http"], 25)

    def test_store_rejects_fractional_prices(self):
        self.login("admin", "another-long-test-password")
        response = self.client.post(
            "/store/manage/products",
            data={
                "name": "Fractional-price item",
                "price": "5.50",
                "description": "",
                "stock_count": "",
                "max_per_team": "",
                "available_until": "",
                "questions": "",
            },
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"whole-dollar amount", response.data)
        self.assertEqual(Product.query.count(), 0)

    def test_store_catalog_filters_are_staff_only(self):
        db.session.add_all(
            [
                Product(name="Alpha item", price=Decimal("5"), published=True),
                Product(name="Beta item", price=Decimal("20"), published=True),
            ]
        )
        db.session.commit()
        filtered_url = "/store/?q=missing&min_price=100&sort=name"

        public_response = self.client.get(filtered_url)
        self.assertEqual(public_response.status_code, 200)
        self.assertNotIn(b"Find an item", public_response.data)
        self.assertIn(b"Alpha item", public_response.data)
        self.assertIn(b"Beta item", public_response.data)

        self.login("blue", "a-long-test-password")
        team_response = self.client.get(filtered_url)
        self.assertEqual(team_response.status_code, 200)
        self.assertNotIn(b"Find an item", team_response.data)
        self.assertIn(b"Alpha item", team_response.data)
        self.assertIn(b"Beta item", team_response.data)

        self.client.post("/logout")
        self.login("admin", "another-long-test-password")
        staff_response = self.client.get("/store/?q=Alpha")
        self.assertEqual(staff_response.status_code, 200)
        self.assertIn(b"Find an item", staff_response.data)
        self.assertIn(b"Alpha item", staff_response.data)
        self.assertNotIn(b"Beta item", staff_response.data)

        for role in ("organizer", "banker"):
            staff_user = User(
                username=f"store-{role}",
                role=role,
            )
            staff_user.set_password("store-staff-test-password")
            db.session.add(staff_user)
            db.session.commit()
            self.client.post("/logout")
            self.login(staff_user.username, "store-staff-test-password")
            response = self.client.get("/store/?q=Alpha")
            self.assertEqual(response.status_code, 200)
            self.assertIn(b"Find an item", response.data)

    def test_bank_api_and_team_pages_use_whole_us_dollars(self):
        db.session.add(
            BankTransaction(
                team=self.team,
                kind="credit",
                amount=Decimal("125"),
                description="Initial dollar balance",
            )
        )
        db.session.add(
            Product(
                name="Dollar item",
                price=Decimal("5"),
                published=True,
            )
        )
        db.session.commit()
        self.login("blue", "a-long-test-password")

        bank_response = self.client.get("/api/v1/me/bank")
        self.assertEqual(bank_response.json["balance"], "125")
        self.assertEqual(bank_response.json["currency"], "USD")
        account_response = self.client.get("/bank/account")
        self.assertIn(b"$125", account_response.data)
        self.assertNotIn(b"credits", account_response.data.lower())
        self.assertNotIn(b">Credit<", account_response.data)
        store_response = self.client.get("/store/")
        self.assertIn(b"$5", store_response.data)
        self.assertNotIn(b"credits", store_response.data.lower())

    def test_bank_and_store_permissions_follow_staff_roles(self):
        banker = User(username="banker", role="banker")
        banker.set_password("banker-long-test-password")
        organizer = User(username="organizer", role="organizer")
        organizer.set_password("organizer-long-test-password")
        db.session.add_all([banker, organizer])
        db.session.commit()

        self.login("banker", "banker-long-test-password")
        bank_page = self.client.get("/bank/")
        self.assertEqual(bank_page.status_code, 200)
        self.assertIn(b"Scoreboard", bank_page.data)
        self.assertLess(
            bank_page.data.index(b"FUND CONTROLS"),
            bank_page.data.index(b"TEAM ACCOUNTS"),
        )
        self.assertEqual(self.client.get("/scoreboard").status_code, 200)
        self.assertEqual(self.client.get("/store/manage/products").status_code, 200)
        self.assertEqual(self.client.get("/store/manage/orders").status_code, 200)
        self.assertEqual(self.client.get("/admin/competitions").status_code, 403)
        self.client.post("/logout")

        self.login("organizer", "organizer-long-test-password")
        self.assertEqual(self.client.get("/bank/").status_code, 403)
        self.assertEqual(self.client.get("/store/manage/products").status_code, 200)
        self.assertEqual(self.client.get("/store/manage/orders").status_code, 200)
        self.assertEqual(self.client.get("/admin/competitions").status_code, 200)
        self.assertEqual(self.client.get("/admin/management-controls").status_code, 403)
        self.assertEqual(
            self.client.post("/admin/competitions/1/unarchive").status_code,
            403,
        )
        dashboard = self.client.get("/admin/")
        self.assertEqual(dashboard.status_code, 200)
        self.assertIn(b"Competition Manager", dashboard.data)
        self.assertNotIn(b">Admin</a>", dashboard.data)
        self.assertNotIn(b"Management controls", dashboard.data)
        competitions_page = self.client.get("/admin/competitions")
        users_page = self.client.get("/admin/users")
        self.assertIn(b"Back to Competition Manager", competitions_page.data)
        self.assertIn(b"Back to Competition Manager", users_page.data)
        self.assertNotIn(b"Back to admin", competitions_page.data)
        self.assertNotIn(b"Back to admin", users_page.data)
        self.client.post("/logout")

        self.login("admin", "another-long-test-password")
        admin_dashboard = self.client.get("/admin/")
        self.assertIn(b">Admin</a>", admin_dashboard.data)
        self.assertIn(b"Management controls", admin_dashboard.data)
        self.assertIn(
            b"Back to admin",
            self.client.get("/admin/competitions").data,
        )

    def test_bank_audit_ledger_loads_only_after_transaction_filters(self):
        transaction = BankTransaction(
            team=self.team,
            kind="credit",
            amount=Decimal("25"),
            description="Filtered audit transaction",
        )
        banker = User(username="ledger-banker", role="banker")
        banker.set_password("banker-long-test-password")
        db.session.add_all([transaction, banker])
        db.session.commit()
        self.login("ledger-banker", "banker-long-test-password")

        with patch("app.bank._apply_transaction_filters") as apply_filters:
            response = self.client.get("/bank/")
            self.assertEqual(response.status_code, 200)
            self.assertIn(
                b"Apply filters to load audit ledger transactions",
                response.data,
            )
            self.assertNotIn(b"Filtered audit transaction", response.data)
            apply_filters.assert_not_called()

            account_filtered = self.client.get("/bank/?account_q=Blue")
            self.assertEqual(account_filtered.status_code, 200)
            apply_filters.assert_not_called()

        filtered_response = self.client.get("/bank/?kind=credit")
        self.assertIn(b"Filtered audit transaction", filtered_response.data)

        invalid_filter_response = self.client.get("/bank/?kind=invalid")
        self.assertNotIn(b"Filtered audit transaction", invalid_filter_response.data)

    def test_bulk_fund_actions_all_teams_preserve_ledger_history(self):
        other_team = Team(name="Red Team")
        other_user = User(username="red", role="team", team=other_team)
        other_user.set_password("a-long-test-password")
        db.session.add_all(
            [
                other_team,
                other_user,
                BankTransaction(
                    team=self.team,
                    kind="credit",
                    amount=Decimal("25.00"),
                    description="Initial Blue funds",
                ),
                BankTransaction(
                    team=other_team,
                    kind="credit",
                    amount=Decimal("45.00"),
                    description="Initial Red funds",
                ),
            ]
        )
        db.session.commit()
        banker = User(username="banker", role="banker")
        banker.set_password("banker-long-test-password")
        db.session.add(banker)
        db.session.commit()
        self.login("banker", "banker-long-test-password")

        response = self.client.post(
            "/bank/adjust",
            data={
                "action": "set",
                "all_teams": "on",
                "amount": "100.00",
                "description": "Opening balance",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(balance_for(self.team.id), Decimal("100.00"))
        self.assertEqual(balance_for(other_team.id), Decimal("100.00"))
        self.assertEqual(BankTransaction.query.count(), 4)

        response = self.client.post(
            "/bank/adjust",
            data={
                "action": "reset",
                "all_teams": "on",
                "description": "Competition reset",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(balance_for(self.team.id), Decimal("0.00"))
        self.assertEqual(balance_for(other_team.id), Decimal("0.00"))
        self.assertEqual(BankTransaction.query.count(), 6)
        self.assertTrue(
            all(
                transaction.created_by_id == banker.id
                for transaction in BankTransaction.query.filter(
                    BankTransaction.description.like("All teams - %")
                )
            )
        )

    def test_bulk_debit_is_atomic_when_a_team_has_insufficient_funds(self):
        other_team = Team(name="Red Team")
        other_user = User(username="red", role="team", team=other_team)
        other_user.set_password("a-long-test-password")
        db.session.add_all(
            [
                other_team,
                other_user,
                BankTransaction(
                    team=self.team,
                    kind="credit",
                    amount=Decimal("10.00"),
                    description="Initial Blue funds",
                ),
                BankTransaction(
                    team=other_team,
                    kind="credit",
                    amount=Decimal("5.00"),
                    description="Initial Red funds",
                ),
            ]
        )
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            "/bank/adjust",
            data={
                "action": "debit",
                "all_teams": "on",
                "amount": "8.00",
                "description": "All-team purchase",
            },
            follow_redirects=True,
        )

        self.assertIn(b"No accounts were changed.", response.data)
        self.assertEqual(balance_for(self.team.id), Decimal("10.00"))
        self.assertEqual(balance_for(other_team.id), Decimal("5.00"))
        self.assertEqual(BankTransaction.query.count(), 2)

    def test_bank_filters_and_transaction_detail_are_team_scoped(self):
        product = Product(
            name="Competition Shirt",
            price=Decimal("5.00"),
            published=True,
        )
        order = Order(
            team=self.team,
            product=product,
            quantity=1,
            unit_price=Decimal("5.00"),
            status="pending",
        )
        question = ProductQuestion(product=product, text="Preferred size")
        order.answers.append(
            OrderAnswer(
                question=question,
                answer="Medium",
            )
        )
        db.session.add(order)
        db.session.flush()
        transaction = BankTransaction(
            team=self.team,
            created_by=self.team_user,
            order_id=order.id,
            kind="debit",
            amount=Decimal("5.00"),
            description="Store purchase: Competition Shirt",
        )
        db.session.add(transaction)
        db.session.commit()

        self.login("admin", "another-long-test-password")
        response = self.client.get(
            "/bank/?account_q=Blue&min_balance=0&q=Competition&kind=debit"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Competition Shirt", response.data)
        self.assertIn(f"/bank/transactions/{transaction.id}".encode(), response.data)

        self.client.post("/logout")
        self.login("blue", "a-long-test-password")
        response = self.client.get(f"/bank/transactions/{transaction.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"RELATED STORE ORDER", response.data)
        self.assertIn(b"Preferred size", response.data)
        self.assertIn(b"Medium", response.data)

        other_team = Team(name="Red Team")
        other_user = User(username="red", role="team", team=other_team)
        other_user.set_password("a-long-test-password")
        db.session.add_all([other_team, other_user])
        db.session.commit()
        self.client.post("/logout")
        self.login("red", "a-long-test-password")
        self.assertEqual(
            self.client.get(f"/bank/transactions/{transaction.id}").status_code,
            404,
        )

    def test_store_filters_apply_to_catalog_items_and_orders(self):
        shirt = Product(
            name="Competition Shirt",
            description="Team apparel",
            price=Decimal("5.00"),
            published=True,
        )
        mug = Product(
            name="Competition Mug",
            price=Decimal("25.00"),
            published=True,
        )
        hidden = Product(
            name="Hidden Shirt",
            price=Decimal("4.00"),
            published=False,
        )
        order = Order(
            team=self.team,
            product=shirt,
            quantity=1,
            unit_price=Decimal("5.00"),
            status="pending",
        )
        db.session.add_all([shirt, mug, hidden, order])
        db.session.commit()

        self.login("blue", "a-long-test-password")
        response = self.client.get("/store/?q=Shirt&max_price=10")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"Find an item", response.data)
        self.assertIn(b"Competition Shirt", response.data)
        self.assertIn(b"Competition Mug", response.data)
        self.assertNotIn(b"Hidden Shirt", response.data)
        response = self.client.get("/store/orders?status=pending&q=Shirt")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Competition Shirt", response.data)

        self.client.post("/logout")
        self.login("admin", "another-long-test-password")
        response = self.client.get("/store/?q=Shirt&max_price=10")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Find an item", response.data)
        self.assertIn(b"Competition Shirt", response.data)
        self.assertNotIn(b"Competition Mug", response.data)
        self.assertNotIn(b"Hidden Shirt", response.data)
        response = self.client.get(
            "/store/manage/products?q=Shirt&visibility=published&max_price=10"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Competition Shirt", response.data)
        self.assertNotIn(b"Hidden Shirt", response.data)
        response = self.client.get(
            f"/store/manage/orders?status=pending&team_id={self.team.id}&product_id={shirt.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Competition Shirt", response.data)

    def test_store_purchase_debits_balance_and_reserves_stock(self):
        db.session.add(
            BankTransaction(
                team_id=self.team.id,
                kind="credit",
                amount=Decimal("25.00"),
                description="Initial competition credits",
            )
        )
        product = Product(
            name="Competition Shirt",
            description="Team merchandise",
            price=Decimal("5.00"),
            stock_count=3,
            max_per_team=2,
            published=True,
        )
        product.questions.append(ProductQuestion(text="Preferred size"))
        db.session.add(product)
        db.session.commit()
        self.login("blue", "a-long-test-password")

        response = self.client.post(
            f"/store/products/{product.id}/purchase",
            data={"quantity": "2", f"question_{product.questions[0].id}": "M"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(balance_for(self.team.id), Decimal("15.00"))
        self.assertEqual(product.stock_count, 1)
        order = Order.query.one()
        self.assertEqual(order.status, "pending")
        self.assertEqual(order.answers[0].answer, "M")

    def test_purchase_rejects_insufficient_funds_without_order(self):
        product = Product(
            name="Limited Item",
            price=Decimal("10.00"),
            stock_count=1,
            published=True,
        )
        db.session.add(product)
        db.session.commit()
        self.login("blue", "a-long-test-password")

        response = self.client.post(
            f"/store/products/{product.id}/purchase", data={"quantity": "1"}
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Order.query.count(), 0)
        self.assertEqual(product.stock_count, 1)

    def test_scoring_tcp_check_succeeds_for_listening_service(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        try:
            passed, details, latency = _probe(
                {
                    "protocol": "tcp",
                    "address": "127.0.0.1",
                    "port": server.getsockname()[1],
                    "timeout": 1,
                }
            )
        finally:
            server.close()
        self.assertTrue(passed, details)
        self.assertIsInstance(latency, int)

    def test_scoring_dns_check_compares_expected_record(self):
        record = type("DNSRecord", (), {"to_text": lambda self: "192.0.2.10"})()
        answer = [record]
        with patch("app.scoring.dns.resolver.Resolver") as resolver_class:
            resolver_class.return_value.resolve.return_value = answer
            passed, details, latency = _probe(
                {
                    "check_type": "dns",
                    "check_config": {
                        "lookup": "service.example.net",
                        "record_type": "A",
                        "expected_output": "192.0.2.10",
                    },
                    "timeout": 1,
                }
            )
        self.assertTrue(passed, details)
        self.assertIsInstance(latency, int)

    def test_scoring_preserves_legacy_https_protocol(self):
        requested = {}

        class Response:
            status = 200

            def read(self, _size):
                return b"healthy"

            def close(self):
                return None

        class Opener:
            def open(self, request, timeout):
                requested["url"] = request.full_url
                return Response()

        with patch("app.scoring.build_opener", return_value=Opener()):
            passed, details, _latency = _probe(
                {
                    "check_type": "http",
                    "check_config": {},
                    "protocol": "https",
                    "address": "service.example.net",
                    "port": 443,
                    "path": "/",
                    "expected_status": 200,
                    "expected_text": None,
                    "timeout": 1,
                }
            )
        self.assertTrue(passed, details)
        self.assertEqual(requested["url"], "https://service.example.net:443/")

    def test_admin_can_create_dns_service_check(self):
        competition = Competition(name="DNS Cup")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "add-service",
                "service_name": "Public DNS",
                "check_type": "dns",
                "lookup": "service.example.net",
                "record_type": "A",
                "expected_output": "192.0.2.10",
                "points": "2",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        service = Service.query.one()
        self.assertEqual(service.check_type, "dns")
        self.assertEqual(service.check_config["lookup"], "service.example.net")

    def test_dns_expected_answer_is_previewed_and_defaulted_when_blank(self):
        competition = Competition(name="DNS Defaults")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        with patch(
            "app.admin._resolve_dns_expected_output",
            return_value="192.0.2.25",
        ) as resolve_answer:
            response = self.client.get(
                "/admin/dns/expected-answer",
                query_string={"lookup": "service.example.net", "record_type": "A"},
            )
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["expected_output"], "192.0.2.25")

            response = self.client.post(
                f"/admin/competitions/{competition.id}",
                data={
                    "action": "add-service",
                    "service_name": "Current DNS answer",
                    "check_type": "dns",
                    "lookup": "service.example.net",
                    "record_type": "A",
                    "expected_output": "",
                    "points": "2",
                    "timeout_seconds": "3",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(resolve_answer.call_count, 2)
        self.assertEqual(
            Service.query.one().check_config["expected_output"],
            "192.0.2.25",
        )

    def test_scoreboard_builder_generates_device_hosts_from_ip_template(self):
        competition = Competition(name="Guided Scoreboard")
        additional_team = Team(name="Red Team")
        additional_team_user = User(
            username="red-team",
            role="team",
            team=additional_team,
        )
        additional_team_user.set_password("red-team-test-password")
        db.session.add_all([competition, additional_team, additional_team_user])
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=groups"
        )
        self.assertEqual(response.status_code, 200)
        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=groups",
            data={
                "action": "save-host-group",
                "step": "groups",
                "host_group_name": "Ubuntu1",
                "check_types": ["http", "ping"],
            },
        )
        self.assertEqual(response.status_code, 302)
        group = HostGroup.query.one()
        self.assertEqual(group.check_types, ["http", "ping"])

        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=templates"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Ending IP octet", response.data)
        self.assertIn(b"Ubuntu1", response.data)

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=templates",
            data={
                "action": "save-group-template",
                "step": "templates",
                "host_group_id": str(group.id),
                "address_prefix": "10.{index}.1",
                "ending_octet": "1",
                "index_start": "10",
                "index_end": "11",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(group.ip_template.address_template, "10.{index}.1.1")

        for team in (self.team, additional_team):
            response = self.client.post(
                f"/admin/competitions/{competition.id}/builder?step=teams",
                data={
                    "action": "enroll-team",
                    "step": "teams",
                    "team_id": str(team.id),
                },
            )
            self.assertEqual(response.status_code, 302)

        self.assertEqual(
            {
                host.team_id: host.address
                for host in Host.query.filter_by(
                    competition_id=competition.id,
                    host_group_id=group.id,
                )
            },
            {
                self.team.id: "10.10.1.1",
                additional_team.id: "10.11.1.1",
            },
        )
        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=teams"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"10.10.1.1", response.data)
        self.assertIn(b"10.11.1.1", response.data)
        self.assertNotIn(b"Ubuntu1 IPs", response.data)
        self.assertIn(b"10.{index}.1.1", response.data)

    def test_builder_saves_all_ip_templates_together_without_partial_updates(self):
        competition = Competition(name="Bulk IP Templates")
        first_group = HostGroup(
            competition=competition,
            name="Linux",
            check_types=["http"],
        )
        second_group = HostGroup(
            competition=competition,
            name="Windows",
            check_types=["ping"],
        )
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=templates"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data.count(b'class="panel form-panel ip-template-card"'), 2)
        self.assertEqual(
            response.data.count(b'name="action" value="save-group-templates"'),
            1,
        )
        self.assertIn(b"Save all IP templates", response.data)

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=templates",
            data={
                "action": "save-group-templates",
                "step": "templates",
                "redirect_step": "teams",
                "host_group_ids": [str(first_group.id), str(second_group.id)],
                f"address_prefix_{first_group.id}": "10.{index}.1",
                f"ending_octet_{first_group.id}": "22",
                f"index_start_{first_group.id}": "10",
                f"index_end_{first_group.id}": "11",
                f"address_prefix_{second_group.id}": "10.{index}.2",
                f"ending_octet_{second_group.id}": "23",
                f"index_start_{second_group.id}": "10",
                f"index_end_{second_group.id}": "11",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("step=teams", response.headers["Location"])
        self.assertEqual(first_group.ip_template.address_template, "10.{index}.1.22")
        self.assertEqual(second_group.ip_template.address_template, "10.{index}.2.23")
        self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=teams",
            data={
                "action": "enroll-team",
                "step": "teams",
                "team_id": str(self.team.id),
            },
        )
        self.assertEqual(
            {
                (host.host_group_id, host.address)
                for host in Host.query.filter_by(competition_id=competition.id)
            },
            {
                (first_group.id, "10.10.1.22"),
                (second_group.id, "10.10.2.23"),
            },
        )

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=templates",
            data={
                "action": "save-group-templates",
                "step": "templates",
                "host_group_ids": [str(first_group.id), str(second_group.id)],
                f"address_prefix_{first_group.id}": "10.{index}.1",
                f"ending_octet_{first_group.id}": "24",
                f"index_start_{first_group.id}": "10",
                f"index_end_{first_group.id}": "11",
                f"address_prefix_{second_group.id}": "10.{index}.2",
                f"ending_octet_{second_group.id}": "256",
                f"index_start_{second_group.id}": "10",
                f"index_end_{second_group.id}": "11",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Windows: The ending IP octet must be between 0 and 255.", response.data)
        self.assertEqual(first_group.ip_template.address_template, "10.{index}.1.22")
        self.assertEqual(second_group.ip_template.address_template, "10.{index}.2.23")
        self.assertEqual(
            {host.address for host in Host.query.filter_by(competition_id=competition.id)},
            {"10.10.1.22", "10.10.2.23"},
        )

    def test_score_check_step_opens_every_missing_group_check_template(self):
        competition = Competition(name="Required Check Templates")
        template = IPTemplate(
            competition=competition,
            name="Ubuntu1 IPs",
            address_template="10.{index}.1.1",
            index_start=10,
            index_end=10,
        )
        host_group = HostGroup(
            competition=competition,
            name="Ubuntu1",
            ip_template=template,
            check_types=["http", "ping"],
        )
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")
        self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=teams",
            data={
                "action": "enroll-team",
                "step": "teams",
                "team_id": str(self.team.id),
            },
        )

        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=checks"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"2 check templates to configure", response.data)
        self.assertIn(b"Configure HTTP / HTTPS for Ubuntu1", response.data)
        self.assertIn(b"Configure PING for Ubuntu1", response.data)
        self.assertIn(b"Ubuntu1", response.data)
        self.assertIn(f'id="required-check-{host_group.id}-http"'.encode(), response.data)
        self.assertNotIn(b"Continue to review", response.data)

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=checks",
            data={
                "action": "add-service",
                "step": "checks",
                "builder_mode": "1",
                "service_name": "Ubuntu1 HTTP / HTTPS",
                "host_group_id": str(host_group.id),
                "check_type": "http",
                "scheme": "http",
                "port": "8080",
                "path": "/health",
                "expected_status": "200",
                "points": "1",
                "points_boost": "0",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=checks"
        )
        self.assertIn(b"1 check templates to configure", response.data)
        self.assertNotIn(b"Configure HTTP / HTTPS for Ubuntu1", response.data)
        self.assertIn(b"Configure PING for Ubuntu1", response.data)
        editing_service = Service.query.filter_by(check_type="http").one()
        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=checks&edit_service={editing_service.id}"
        )
        self.assertLess(
            response.data.index(b"REQUIRED CONFIGURATION"),
            response.data.index(b"DEVICE GROUP PROGRESS"),
        )
        self.assertIn(b"Save or cancel the current check edit", response.data)
        self.assertEqual(response.data.count(b">Save check</button>"), 1)
        self.assertNotIn(b"Configure PING for Ubuntu1", response.data)

        self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=checks",
            data={
                "action": "add-service",
                "step": "checks",
                "builder_mode": "1",
                "service_name": "Ubuntu1 PING",
                "host_group_id": str(host_group.id),
                "check_type": "ping",
                "attempts": "1",
                "points": "1",
                "points_boost": "0",
                "timeout_seconds": "3",
            },
        )
        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=checks"
        )
        self.assertIn(b"Continue to review", response.data)
        self.assertIn(b"2 of 2 required checks configured", response.data)
        self.assertIn(b"Complete", response.data)
        self.assertNotIn(b"Add score check", response.data)
        self.assertNotIn(b"Open full competition settings", response.data)
        self.assertNotIn(b"Configured by device group", response.data)

        paused_service = Service.query.filter_by(check_type="http").one()
        paused_service.paused = True
        db.session.commit()
        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=checks"
        )
        self.assertIn(b"Needs attention", response.data)
        self.assertIn(b"Paused", response.data)
        self.assertNotIn(b"Continue to review", response.data)

        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=checks&edit_service={paused_service.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'id="service-form"', response.data)
        self.assertIn(b"Enable this score check", response.data)

    def test_scoring_check_targets_its_selected_device_group(self):
        competition = Competition(
            name="Host Group Scoring",
            status="active",
        )
        additional_team = Team(name="Red Team")
        additional_team_user = User(
            username="scoring-red-team",
            role="team",
            team=additional_team,
        )
        additional_team_user.set_password("red-team-test-password")
        template = IPTemplate(
            competition=competition,
            name="subnet",
            address_template="10.{index}.1.1",
            index_start=10,
            index_end=10,
        )
        host_group = HostGroup(
            competition=competition,
            name="Ubuntu1",
            ip_template=template,
            check_types=["http"],
        )
        competition.teams.extend([self.team, additional_team])
        db.session.add_all([competition, additional_team, additional_team_user])
        db.session.flush()
        db.session.add_all(
            [
                Host(
                    competition=competition,
                    team=self.team,
                    address="127.0.0.1",
                ),
                Host(
                    competition=competition,
                    team=self.team,
                    host_group=host_group,
                    address="127.0.0.2",
                ),
                Host(
                    competition=competition,
                    team=additional_team,
                    host_group=host_group,
                    address="127.0.0.3",
                ),
                Service(
                    competition=competition,
                    name="Grouped web check",
                    check_type="http",
                    protocol="http",
                    port=80,
                    host_group=host_group,
                ),
            ]
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="queued",
        )
        db.session.add(score_round)
        db.session.commit()
        with patch(
            "app.scoring._probe",
            return_value=(True, "Check passed.", 2),
        ) as probe:
            process_scoring_round(score_round.id, self.app)

        self.assertEqual(
            {call.args[0]["address"] for call in probe.call_args_list},
            {"127.0.0.2", "127.0.0.3"},
        )

    def test_score_check_can_be_created_from_builder_tab(self):
        competition = Competition(name="Builder Check Tab")
        template = IPTemplate(
            competition=competition,
            name="subnet",
            address_template="10.{index}.1.1",
            index_start=10,
            index_end=10,
        )
        host_group = HostGroup(
            competition=competition,
            name="Ubuntu1",
            ip_template=template,
            check_types=["http"],
        )
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=teams",
            data={
                "action": "enroll-team",
                "step": "teams",
                "team_id": str(self.team.id),
            },
        )
        self.assertEqual(response.status_code, 302)
        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=checks"
        )
        self.assertEqual(response.status_code, 200)

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=checks",
            data={
                "action": "add-service",
                "step": "checks",
                "builder_mode": "1",
                "service_name": "Web health",
                "host_group_id": str(host_group.id),
                "check_type": "http",
                "scheme": "http",
                "subdomain": "",
                "port": "8080",
                "path": "/health",
                "expected_status": "200",
                "expected_text": "",
                "points": "4",
                "points_boost": "0",
                "timeout_seconds": "3",
                "service_group_id": "",
            },
        )
        self.assertEqual(response.status_code, 302)
        service = Service.query.one()
        self.assertEqual(service.host_group_id, host_group.id)
        self.assertEqual(service.name, "Web health")
        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=checks",
            data={
                "action": "add-service",
                "step": "checks",
                "builder_mode": "1",
                "service_name": "Disallowed SSH",
                "host_group_id": str(host_group.id),
                "check_type": "ssh",
                "points": "1",
                "points_boost": "0",
                "timeout_seconds": "3",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Select a check type enabled for this device group.", response.data)
        self.assertEqual(Service.query.count(), 1)
        response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=review"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn('aria-label="Complete">✓'.encode(), response.data)

    def test_builder_rejects_invalid_ip_template(self):
        competition = Competition(name="Invalid Template")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=groups",
            data={
                "action": "save-host-group",
                "step": "groups",
                "host_group_name": "Invalid Group",
                "check_types": ["http"],
            },
        )
        self.assertEqual(response.status_code, 302)
        group = HostGroup.query.one()

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=templates",
            data={
                "action": "save-group-template",
                "step": "templates",
                "host_group_id": str(group.id),
                "address_prefix": "10.{index}.1",
                "ending_octet": "256",
                "index_start": "1",
                "index_end": "3",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"ending IP octet must be between 0 and 255", response.data)
        self.assertEqual(IPTemplate.query.count(), 0)

    def test_builder_rejects_device_group_from_another_scoreboard(self):
        competition = Competition(name="First Scoreboard")
        other_competition = Competition(name="Other Scoreboard")
        template = IPTemplate(
            competition=other_competition,
            name="other subnet",
            address_template="10.2.{index}.1",
            index_start=1,
            index_end=2,
        )
        host_group = HostGroup(
            competition=other_competition,
            name="Other Linux",
            ip_template=template,
            check_types=["http"],
        )
        db.session.add_all([competition, other_competition])
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=checks",
            data={
                "action": "add-service",
                "step": "checks",
                "builder_mode": "1",
                "service_name": "Web health",
                "host_group_id": str(host_group.id),
                "check_type": "http",
                "scheme": "http",
                "port": "8080",
                "path": "/health",
                "expected_status": "200",
                "points": "4",
                "points_boost": "0",
                "timeout_seconds": "3",
            },
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Choose a device group from this scoreboard.", response.data)
        self.assertEqual(Service.query.count(), 0)

    def test_builder_blocks_activation_until_setup_is_complete(self):
        competition = Competition(name="Incomplete Scoreboard")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=review",
            data={"action": "activate", "step": "review"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(competition.status, "draft")
        self.assertIn(b"Complete every setup tab", response.data)

    def test_admin_can_create_service_group_and_reject_duplicate_name(self):
        competition = Competition(name="Grouped Checks")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        payload = {
            "action": "add-service-group",
            "service_group_name": "web",
            "service_group_display_name": "Web Services",
        }
        response = self.client.post(
            f"/admin/competitions/{competition.id}", data=payload
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(ServiceGroup.query.count(), 1)
        self.assertEqual(ServiceGroup.query.one().display_name, "Web Services")

        self.client.post(f"/admin/competitions/{competition.id}", data=payload)
        self.assertEqual(ServiceGroup.query.count(), 1)

    def test_admin_service_group_assignment_is_competition_scoped(self):
        competition = Competition(name="Grouped Checks")
        other_competition = Competition(name="Other Competition")
        group = ServiceGroup(
            competition=competition,
            name="web",
            display_name="Web Services",
        )
        other_group = ServiceGroup(
            competition=other_competition,
            name="web",
            display_name="Other Web Services",
        )
        service = Service(
            competition=competition,
            name="DNS service",
            check_type="dns",
            check_config={
                "lookup": "service.example.net",
                "record_type": "A",
                "expected_output": "192.0.2.10",
            },
            protocol="dns",
            port=53,
        )
        db.session.add_all(
            [competition, other_competition, group, other_group, service]
        )
        db.session.commit()
        self.login("admin", "another-long-test-password")

        payload = {
            "action": "update-service",
            "service_id": str(service.id),
            "check_type": "dns",
            "service_name": "DNS service",
            "lookup": "service.example.net",
            "record_type": "A",
            "expected_output": "192.0.2.10",
            "points": "1",
            "points_boost": "2",
            "timeout_seconds": "3",
            "paused": "on",
            "hidden_from_scoreboard": "on",
        }
        payload["service_group_id"] = str(group.id)
        response = self.client.post(
            f"/admin/competitions/{competition.id}", data=payload
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(service.service_group_id, group.id)
        self.assertEqual(service.points_boost, 2)
        self.assertTrue(service.paused)
        self.assertTrue(service.hidden_from_scoreboard)

        payload["service_group_id"] = str(other_group.id)
        self.client.post(f"/admin/competitions/{competition.id}", data=payload)
        self.assertEqual(service.service_group_id, group.id)

    def test_admin_can_edit_http_check_and_prefills_existing_values(self):
        competition = Competition(name="Editable Checks")
        service = Service(
            competition=competition,
            name="Web health",
            check_type="http",
            check_config={"scheme": "https", "subdomain": "www"},
            protocol="https",
            port=443,
            path="/health",
            expected_status=204,
            expected_text="ready",
            points=4,
            timeout_seconds=6,
        )
        db.session.add(service)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get(
            f"/admin/competitions/{competition.id}?edit_service={service.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="Web health"', response.data)
        self.assertIn(b'value="/health"', response.data)
        self.assertIn(b'value="443"', response.data)
        self.assertIn(b'value="ready"', response.data)
        self.assertIn(b"Save check", response.data)

        response = self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "update-service",
                "service_id": str(service.id),
                "check_type": "http",
                "service_name": "Web health updated",
                "scheme": "http",
                "subdomain": "status",
                "port": "8080",
                "path": "/ready",
                "expected_status": "200",
                "expected_text": "healthy",
                "points": "7",
                "timeout_seconds": "9",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(Service.query.count(), 1)
        self.assertEqual(service.name, "Web health updated")
        self.assertEqual(service.protocol, "http")
        self.assertEqual(service.check_config["subdomain"], "status")
        self.assertEqual(service.path, "/ready")
        self.assertEqual(service.points, 7)
        self.assertEqual(service.timeout_seconds, 9)

    def test_admin_edit_preserves_credentials_when_password_fields_are_blank(self):
        competition = Competition(name="Editable Checks")
        token = encrypt_credentials("monitor", "preserved-password")
        service = Service(
            competition=competition,
            name="SSH service",
            check_type="ssh",
            check_config={
                "credentials": token,
                "host_key_fingerprint": "SHA256:ZmFrZS1maW5nZXJwcmludA",
            },
            protocol="ssh",
            port=22,
        )
        db.session.add(service)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get(
            f"/admin/competitions/{competition.id}?edit_service={service.id}"
        )
        self.assertIn(b"Teams can update this check", response.data)
        response = self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "update-service",
                "service_id": str(service.id),
                "check_type": "ssh",
                "service_name": "SSH service",
                "username": "",
                "password": "",
                "host_key_fingerprint": "SHA256:ZmFrZS1maW5nZXJwcmludA",
                "port": "2222",
                "points": "1",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(service.check_config["credentials"], token)
        self.assertEqual(service.team_editable_fields, [])
        self.assertEqual(service.port, 2222)

    def test_admin_edit_changes_only_ssh_password_or_username(self):
        competition = Competition(name="Editable Checks")
        service = Service(
            competition=competition,
            name="SSH service",
            check_type="ssh",
            check_config={
                "credentials": encrypt_credentials("monitor", "old-password"),
                "host_key_verification": "skip",
            },
            protocol="ssh",
            port=22,
        )
        db.session.add(service)
        db.session.commit()
        self.login("admin", "another-long-test-password")
        base = {
            "action": "update-service",
            "service_id": str(service.id),
            "check_type": "ssh",
            "service_name": "SSH service",
            "skip_host_key_verification": "on",
            "port": "22",
            "points": "1",
            "timeout_seconds": "3",
        }
        url = f"/admin/competitions/{competition.id}"

        self.client.post(url, data={**base, "username": "monitor", "password": "new-password-1"})
        self.assertEqual(
            decrypt_credentials(service.check_config["credentials"]),
            ("monitor", "new-password-1"),
        )
        self.client.post(url, data={**base, "username": "scorer", "password": ""})
        self.assertEqual(
            decrypt_credentials(service.check_config["credentials"]),
            ("scorer", "new-password-1"),
        )
        page = self.client.get(f"{url}?edit_service={service.id}")
        self.assertIn(b'data-saved-username value="scorer"', page.data)

    def test_admin_edit_rejects_check_type_changes(self):
        competition = Competition(name="Editable Checks")
        service = Service(
            competition=competition,
            name="DNS service",
            check_type="dns",
            check_config={
                "lookup": "service.example.net",
                "record_type": "A",
                "expected_output": "192.0.2.10",
            },
            protocol="dns",
            port=53,
        )
        db.session.add(service)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "update-service",
                "service_id": str(service.id),
                "check_type": "http",
                "service_name": "Changed type",
                "points": "1",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(service.check_type, "dns")
        self.assertEqual(service.name, "DNS service")

    def test_admin_can_edit_legacy_tcp_check(self):
        competition = Competition(name="Legacy TCP")
        service = Service(
            competition=competition,
            name="Legacy socket",
            check_type="tcp",
            protocol="tcp",
            port=8080,
        )
        db.session.add(service)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get(
            f"/admin/competitions/{competition.id}?edit_service={service.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b'value="tcp"', response.data)
        response = self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "update-service",
                "service_id": str(service.id),
                "check_type": "tcp",
                "service_name": "Legacy socket",
                "port": "8081",
                "points": "1",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        self.assertEqual(service.port, 8081)

    def test_admin_edit_form_renders_for_every_supported_check_type(self):
        competition = Competition(name="All editable checks")
        credential_token = encrypt_credentials("monitor", "check-password")
        configs = {
            "http": {"scheme": "https", "subdomain": "www"},
            "dns": {
                "lookup": "service.example.net",
                "record_type": "A",
                "expected_output": "192.0.2.10",
            },
            "ping": {"attempts": 2},
            "ssh": {
                "credentials": credential_token,
                "host_key_fingerprint": "SHA256:ZmFrZS1maW5nZXJwcmludA",
            },
            "winrm": {
                "credentials": credential_token,
                "auth_transport": "ntlm",
            },
            "ldap": {
                "credentials": credential_token,
                "base_dn": "dc=example,dc=net",
                "search_filter": "(objectClass=person)",
                "attributes": ["cn"],
                "expected_output": "monitor",
            },
            "sql": {
                "credentials": credential_token,
                "database_name": "competition",
            },
            "ftp": {
                "credentials": credential_token,
                "read_filename": "status.txt",
                "expected_output": "ready",
            },
            "tcp": {},
        }
        services = []
        for check_type, config in configs.items():
            service = Service(
                competition=competition,
                name=f"{check_type} check",
                check_type=check_type,
                check_config=config,
                protocol="https" if check_type == "http" else check_type,
                port=443 if check_type == "http" else 5986 if check_type == "winrm" else 1,
            )
            services.append(service)
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        for service in services:
            with self.subTest(check_type=service.check_type):
                response = self.client.get(
                    f"/admin/competitions/{competition.id}?edit_service={service.id}"
                )
                self.assertEqual(response.status_code, 200)
                self.assertIn(b"Edit service check", response.data)
                self.assertIn(
                    f'data-check-config="{service.check_type}"'.encode(),
                    response.data,
                )

    def test_competition_check_form_hides_unsupported_check_types(self):
        competition = Competition(name="Check Catalog")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get(f"/admin/competitions/{competition.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"DNS lookup", response.data)
        self.assertIn(b"SSH authentication", response.data)
        self.assertIn(b"WinRM HTTPS authentication", response.data)
        self.assertNotIn(b'value="tcp"', response.data)
        for label in (b"SMB", b"IMAP", b"CalDav", b"LEGACY CHECK CATALOG"):
            self.assertNotIn(label, response.data)

        builder_response = self.client.get(
            f"/admin/competitions/{competition.id}/builder?step=groups"
        )
        self.assertEqual(builder_response.status_code, 200)
        for label in (b"SMB", b"IMAP", b"CalDav", b"not supported yet"):
            self.assertNotIn(label, builder_response.data)

    def test_competition_detail_removes_redundant_controls_and_reorders_standings(self):
        competition = Competition(name="Clean Competition")
        host_group = HostGroup(competition=competition, name="Linux")
        service_group = ServiceGroup(
            competition=competition,
            name="hidden-group",
            display_name="Hidden Service Group",
        )
        service = Service(
            competition=competition,
            host_group=host_group,
            service_group=service_group,
            name="Web health",
            check_type="http",
            check_config={"scheme": "http"},
            protocol="http",
            port=80,
            path="/",
        )
        competition.teams.append(self.team)
        db.session.add_all([competition, host_group, service_group, service])
        db.session.flush()
        db.session.add(
            Host(
                competition=competition,
                team=self.team,
                host_group=host_group,
                address="10.1.1.20",
            )
        )
        additional_team = Team(name="Red Team")
        additional_user = User(username="red", role="team", team=additional_team)
        additional_user.set_password("another-long-test-password")
        db.session.add_all([additional_team, additional_user])
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get(
            f"/admin/competitions/{competition.id}?edit_service={service.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"SERVICE ORGANIZATION", response.data)
        self.assertNotIn(b"Service groups", response.data)
        self.assertNotIn(b"Hidden Service Group", response.data)
        self.assertNotIn(b"Scoring host</th>", response.data)
        self.assertNotIn(b"Update host", response.data)
        self.assertNotIn(b"COMPETITION TEAMS", response.data)
        self.assertNotIn(b'id="address"', response.data)
        self.assertIn(
            f'name="service_group_id" value="{service_group.id}"'.encode(),
            response.data,
        )
        self.assertLess(
            response.data.index(b"COMPETITOR SETUP"),
            response.data.index(b"LIVE STANDINGS"),
        )
        self.assertLess(
            response.data.index(b"LIVE STANDINGS"),
            response.data.index(b"SCORING CONFIGURATION"),
        )
        self.assertLess(
            response.data.index(b"LIVE STANDINGS"),
            response.data.index(b"SERVICE CHECKS"),
        )
        self.assertIn(b"Duration", response.data)
        self.assertIn(b"competition-rounds-live.js", response.data)

    def test_admin_can_fetch_live_scoring_round_history(self):
        competition = Competition(name="Live Round History")
        score_round = ScoreRound(
            competition=competition,
            number=4,
            status="running",
            started_at=datetime(2026, 1, 2, 3, 4, 5, tzinfo=timezone.utc),
        )
        db.session.add_all([competition, score_round])
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.get(
            f"/admin/competitions/{competition.id}/rounds/live"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers["Cache-Control"], "no-store")
        self.assertEqual(response.json["rounds"][0]["number"], 4)
        self.assertEqual(response.json["rounds"][0]["status"], "running")
        self.assertEqual(
            response.json["rounds"][0]["started_at"],
            "2026-01-02T03:04:05+00:00",
        )
        self.assertEqual(
            response.json["rounds"][0]["detail"], "Scoring in progress"
        )

    def test_admin_adds_winrm_authentication_without_storing_command(self):
        competition = Competition(name="Safe Checks")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "add-service",
                "service_name": "WinRM service",
                "check_type": "winrm",
                "username": "monitor",
                "password": "winrm-check-password",
                "auth_transport": "ntlm",
                "port": "5986",
                "command": "whoami",
                "points": "1",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        service = Service.query.one()
        self.assertEqual(service.protocol, "https")
        self.assertEqual(service.port, 5986)
        self.assertEqual(service.check_config["auth_transport"], "ntlm")
        self.assertNotIn("command", service.check_config)
        self.assertEqual(
            decrypt_credentials(service.check_config["credentials"]),
            ("monitor", "winrm-check-password"),
        )

    def test_admin_can_save_opt_in_legacy_check_modes(self):
        competition = Competition(name="Legacy Modes")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")
        base = {"action": "add-service", "points": "2", "timeout_seconds": "5"}
        url = f"/admin/competitions/{competition.id}"
        cases = [
            {"service_name": "SSH", "check_type": "ssh", "username": "u", "password": "p", "port": "22",
             "skip_host_key_verification": "on", "legacy_mode": "on",
             "legacy_command": "whoami", "legacy_expected_output": "u"},
            {"service_name": "WinRM", "check_type": "winrm", "username": "u", "password": "p", "port": "5985",
             "auth_transport": "ntlm", "legacy_mode": "on", "legacy_scheme": "http",
             "legacy_command": "whoami", "legacy_expected_output": "u"},
            {"service_name": "LDAP", "check_type": "ldap", "username": "u", "password": "p", "port": "389",
             "base_dn": "", "search_filter": "(objectClass=person)", "attributes": "dn,cn",
             "legacy_mode": "on", "legacy_domain": "example.local"},
            {"service_name": "SQL", "check_type": "sql", "username": "u", "password": "p", "port": "3306",
             "database_name": "app", "legacy_mode": "on", "legacy_sql_command": "SELECT * FROM users;",
             "legacy_min_rows": "4", "legacy_max_rows": "6"},
            {"service_name": "FTP", "check_type": "ftp", "username": "u", "password": "p", "port": "21",
             "read_filename": "Message.txt", "expected_output": "hi", "legacy_mode": "on",
             "legacy_write_filename": "Message.txt", "legacy_write_text": "hi"},
            {"service_name": "DNS", "check_type": "dns", "lookup": "corp.example", "record_type": "A",
             "legacy_mode": "on"},
        ]
        for case in cases:
            self.assertEqual(self.client.post(url, data={**base, **case}).status_code, 302, case["service_name"])
        configs = {s.name: s.check_config for s in Service.query.all()}
        self.assertEqual(configs["SSH"]["command"], "whoami")
        self.assertEqual(configs["WinRM"]["scheme"], "http")
        self.assertEqual(Service.query.filter_by(name="WinRM").one().protocol, "http")
        self.assertEqual(configs["LDAP"]["base_dn"], "dc=example,dc=local")
        self.assertFalse(configs["LDAP"]["use_ssl"])
        self.assertEqual(configs["SQL"]["command"], "SELECT * FROM users")
        self.assertEqual((configs["SQL"]["min_rows"], configs["SQL"]["max_rows"]), (4, 6))
        self.assertFalse(configs["FTP"]["use_tls"])
        self.assertEqual(configs["DNS"]["expected_output"], "{address}")
        self.assertTrue(configs["DNS"]["query_host"])

    def test_legacy_sql_rejects_non_select_queries(self):
        competition = Competition(name="Legacy SQL")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")
        self.client.post(
            f"/admin/competitions/{competition.id}",
            data={"action": "add-service", "service_name": "SQL", "check_type": "sql", "username": "u",
                  "password": "p", "port": "3306", "database_name": "app", "legacy_mode": "on",
                  "legacy_sql_command": "DROP TABLE users", "legacy_min_rows": "0",
                  "legacy_max_rows": "1", "points": "1", "timeout_seconds": "3"},
        )
        self.assertEqual(Service.query.count(), 0)

    def test_dns_check_can_query_host_and_match_its_address(self):
        class Record:
            def to_text(self):
                return "10.4.1.60"

        job = {"check_type": "dns", "address": "10.4.1.60", "port": 53, "timeout": 2,
               "check_config": {"lookup": "corp.example", "record_type": "A",
                                "expected_output": "{address}", "query_host": True}}
        with patch("app.scoring.dns.resolver.Resolver") as resolver_class:
            resolver = resolver_class.return_value
            resolver.resolve.return_value = [Record()]
            passed, details, _latency = _probe(job)
        self.assertTrue(passed, details)
        self.assertEqual(resolver.nameservers, ["10.4.1.60"])

    def test_admin_can_skip_ssh_host_key_fingerprint_explicitly(self):
        competition = Competition(name="SSH Without Fingerprint")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "add-service",
                "service_name": "SSH service",
                "check_type": "ssh",
                "username": "monitor",
                "password": "ssh-check-password",
                "port": "22",
                "skip_host_key_verification": "on",
                "points": "1",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        service = Service.query.one()
        self.assertEqual(service.check_config["host_key_verification"], "skip")
        self.assertNotIn("host_key_fingerprint", service.check_config)

    def test_ssh_without_fingerprint_requires_explicit_skip_setting(self):
        competition = Competition(name="SSH Verification Required")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "add-service",
                "service_name": "SSH service",
                "check_type": "ssh",
                "username": "monitor",
                "password": "ssh-check-password",
                "port": "22",
                "points": "1",
                "timeout_seconds": "3",
            },
            follow_redirects=True,
        )
        self.assertIn(b"Enter the SSH server&#39;s SHA256 host-key fingerprint", response.data)
        self.assertEqual(Service.query.count(), 0)

    def test_winrm_rejects_unsupported_authentication_transport(self):
        competition = Competition(name="Safe Checks")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "add-service",
                "service_name": "WinRM service",
                "check_type": "winrm",
                "username": "monitor",
                "password": "winrm-check-password",
                "auth_transport": "plaintext",
                "points": "1",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(Service.query.count(), 0)

    def test_check_credentials_support_legacy_secret_key_encryption(self):
        legacy_key = hashlib.sha256(
            b"competition-check-credentials\0" + b"test-key"
        ).digest()
        legacy_fernet = Fernet(base64.urlsafe_b64encode(legacy_key))
        token = legacy_fernet.encrypt(
            json.dumps(
                {"username": "legacy-monitor", "password": "legacy-password"}
            ).encode("utf-8")
        ).decode("ascii")
        self.assertEqual(
            decrypt_credentials(token),
            ("legacy-monitor", "legacy-password"),
        )

    def test_check_credentials_are_encrypted_in_service_configuration(self):
        competition = Competition(name="SSH Checks")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}",
            data={
                "action": "add-service",
                "service_name": "SSH service",
                "check_type": "ssh",
                "username": "monitor",
                "password": "top-secret-test-password",
                "host_key_fingerprint": "SHA256:ZmFrZS1maW5nZXJwcmludA",
                "port": "2222",
                "points": "1",
                "timeout_seconds": "3",
            },
        )
        self.assertEqual(response.status_code, 302)
        service = Service.query.one()
        token = service.check_config["credentials"]
        self.assertNotIn("monitor", token)
        self.assertNotIn("top-secret-test-password", token)
        self.assertEqual(
            decrypt_credentials(token),
            ("monitor", "top-secret-test-password"),
        )

    def test_scoring_round_runs_dns_check_without_team_host(self):
        competition = Competition(name="DNS Cup", status="active")
        competition.teams.append(self.team)
        db.session.add(competition)
        db.session.flush()
        service = Service(
            competition_id=competition.id,
            name="Public DNS",
            check_type="dns",
            check_config={
                "lookup": "service.example.net",
                "record_type": "A",
                "expected_output": "192.0.2.10",
            },
            protocol="dns",
            port=0,
            points=2,
        )
        score_round = ScoreRound(
            competition_id=competition.id,
            number=1,
            status="queued",
        )
        db.session.add_all([service, score_round])
        db.session.commit()
        record = type("DNSRecord", (), {"to_text": lambda self: "192.0.2.10"})()

        with patch("app.scoring.dns.resolver.Resolver") as resolver_class:
            resolver_class.return_value.resolve.return_value = [record]
            process_scoring_round(score_round.id, self.app)

        db.session.expire_all()
        result = ScoreResult.query.one()
        self.assertTrue(result.passed)
        self.assertEqual(result.points, 2)

    def test_ping_check_uses_bounded_subprocess_arguments(self):
        with patch("app.scoring.shutil.which", return_value="ping"), patch(
            "app.scoring.subprocess.run"
        ) as run:
            run.return_value.returncode = 0
            passed, details, _latency = _probe(
                {
                    "check_type": "ping",
                    "check_config": {"attempts": 2},
                    "address": "127.0.0.1",
                    "timeout": 1,
                }
            )
        self.assertTrue(passed, details)
        args, kwargs = run.call_args
        self.assertIn("--", args[0])
        self.assertEqual(args[0][-1], "127.0.0.1")
        self.assertNotIn("shell", kwargs)
        self.assertEqual(kwargs["timeout"], 4)

    def test_ssh_check_pins_host_key_and_does_not_open_shell(self):
        key_bytes = b"example ssh public host key"
        fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(key_bytes).digest()
        ).decode("ascii").rstrip("=")

        class FakeKey:
            def asbytes(self):
                return key_bytes

        class FakeTransport:
            def __init__(self, _sock):
                self.authenticated = False

            def start_client(self, timeout):
                self.timeout = timeout

            def get_remote_server_key(self):
                return FakeKey()

            def auth_password(self, username, password, fallback):
                self.authenticated = (username, password, fallback) == (
                    "monitor",
                    "ssh-check-password",
                    False,
                )

            def is_authenticated(self):
                return self.authenticated

            def close(self):
                return None

        token = encrypt_credentials("monitor", "ssh-check-password")
        with patch("app.scoring.socket.create_connection"), patch(
            "app.scoring.paramiko.Transport", FakeTransport
        ):
            passed, details, _latency = _probe(
                {
                    "check_type": "ssh",
                    "check_config": {
                        "credentials": token,
                        "host_key_fingerprint": fingerprint,
                    },
                    "address": "127.0.0.1",
                    "port": 22,
                    "timeout": 1,
                }
            )
        self.assertTrue(passed, details)

    def test_ssh_check_can_skip_fingerprint_only_when_explicitly_configured(self):
        class FakeKey:
            def asbytes(self):
                return b"example ssh public host key"

        class FakeTransport:
            def __init__(self, _sock):
                self.authenticated = False

            def start_client(self, timeout):
                self.timeout = timeout

            def get_remote_server_key(self):
                return FakeKey()

            def auth_password(self, username, password, fallback):
                self.authenticated = (username, password, fallback) == (
                    "monitor",
                    "ssh-check-password",
                    False,
                )

            def is_authenticated(self):
                return self.authenticated

            def close(self):
                return None

        token = encrypt_credentials("monitor", "ssh-check-password")
        job = {
            "check_type": "ssh",
            "check_config": {
                "credentials": token,
                "host_key_verification": "skip",
            },
            "address": "127.0.0.1",
            "port": 22,
            "timeout": 1,
        }
        with patch("app.scoring.socket.create_connection"), patch(
            "app.scoring.paramiko.Transport", FakeTransport
        ):
            passed, details, _latency = _probe(job)
        self.assertTrue(passed, details)
        self.assertIn("host key was not verified", details)

        job["check_config"].pop("host_key_verification")
        with patch("app.scoring.socket.create_connection"), patch(
            "app.scoring.paramiko.Transport", FakeTransport
        ):
            passed, details, _latency = _probe(job)
        self.assertFalse(passed)
        self.assertIn("verification is not configured", details)

    def test_scoring_probe_decrypts_credentials_in_worker_thread(self):
        key_bytes = b"example ssh public host key"
        fingerprint = "SHA256:" + base64.b64encode(
            hashlib.sha256(key_bytes).digest()
        ).decode("ascii").rstrip("=")

        class FakeKey:
            def asbytes(self):
                return key_bytes

        class FakeTransport:
            def __init__(self, _sock):
                self.authenticated = False

            def start_client(self, timeout):
                self.timeout = timeout

            def get_remote_server_key(self):
                return FakeKey()

            def auth_password(self, username, password, fallback):
                self.authenticated = (username, password, fallback) == (
                    "monitor",
                    "ssh-check-password",
                    False,
                )

            def is_authenticated(self):
                return self.authenticated

            def close(self):
                return None

        token = encrypt_credentials("monitor", "ssh-check-password")
        job = {
            "check_type": "ssh",
            "check_config": {
                "credentials": token,
                "host_key_fingerprint": fingerprint,
            },
            "credential_key": self.app.config["CHECK_CREDENTIAL_KEY"],
            "secret_key": self.app.config["SECRET_KEY"],
            "address": "127.0.0.1",
            "port": 22,
            "timeout": 1,
        }
        with patch("app.scoring.socket.create_connection"), patch(
            "app.scoring.paramiko.Transport", FakeTransport
        ):
            with ThreadPoolExecutor(max_workers=1) as executor:
                passed, details, _latency = executor.submit(_probe, job).result()
        self.assertTrue(passed, details)

    def test_mysql_check_executes_only_fixed_health_query(self):
        class Cursor:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def execute(self, query):
                self.query = query

            def fetchone(self):
                self.executed_query = self.query
                return (1,)

        cursor = Cursor()

        class Connection:
            def cursor(self):
                return cursor

            def close(self):
                return None

        token = encrypt_credentials("monitor", "mysql-check-password")
        with patch("app.scoring.pymysql.connect", return_value=Connection()) as connect:
            passed, details, _latency = _probe(
                {
                    "check_type": "sql",
                    "check_config": {
                        "credentials": token,
                        "database_name": "competition",
                    },
                    "address": "127.0.0.1",
                    "port": 3306,
                    "timeout": 1,
                }
            )
        self.assertTrue(passed, details)
        self.assertEqual(cursor.executed_query, "SELECT 1")
        self.assertTrue(connect.call_args.kwargs["ssl"].check_hostname)

    def test_scoring_winrm_opens_and_closes_shell_over_validated_https(self):
        protocol_instances = []

        class FakeProtocol:
            def __init__(self, **kwargs):
                self.options = kwargs
                protocol_instances.append(self)

            def open_shell(self):
                return "temporary-shell-id"

            def close_shell(self, shell_id):
                self.closed_shell = shell_id

        token = encrypt_credentials("DOMAIN\\monitor", "winrm-check-password")
        with patch("app.scoring.Protocol", FakeProtocol):
            passed, details, _latency = _probe(
                {
                    "check_type": "winrm",
                    "check_config": {
                        "credentials": token,
                        "auth_transport": "ntlm",
                    },
                    "address": "server.example.org",
                    "port": 5986,
                    "timeout": 3,
                }
            )
        self.assertTrue(passed, details)
        self.assertEqual(
            protocol_instances[0].options["endpoint"],
            "https://server.example.org:5986/wsman",
        )
        self.assertEqual(
            protocol_instances[0].options["server_cert_validation"], "validate"
        )
        self.assertEqual(protocol_instances[0].options["read_timeout_sec"], 5)
        self.assertEqual(protocol_instances[0].closed_shell, "temporary-shell-id")

    def test_scoring_winrm_reports_timeout_without_claiming_success(self):
        class FakeProtocol:
            def __init__(self, **_kwargs):
                pass

            def open_shell(self):
                raise requests.exceptions.ReadTimeout()

        with patch("app.scoring.Protocol", FakeProtocol):
            passed, details, _latency = _probe(
                {
                    "check_type": "winrm",
                    "check_config": {
                        "credentials": encrypt_credentials(
                            "monitor", "winrm-check-password"
                        ),
                        "auth_transport": "ntlm",
                    },
                    "address": "server.example.org",
                    "port": 5986,
                    "timeout": 3,
                }
            )
        self.assertFalse(passed)
        self.assertIn("timed out", details)

    def test_ldap_check_uses_verified_tls_and_bounded_search(self):
        class Entry:
            def entry_to_json(self):
                return '{"attributes":{"cn":["competition-service"]}}'

        class Connection:
            entries = [Entry()]

            def __init__(self, _server, **kwargs):
                self.options = kwargs

            def search(self, **kwargs):
                self.search_options = kwargs
                return True

            def unbind(self):
                return True

        token = encrypt_credentials("monitor", "ldap-check-password")
        with patch("app.scoring.ldap3.Server") as server, patch(
            "app.scoring.ldap3.Connection", Connection
        ):
            passed, details, _latency = _probe(
                {
                    "check_type": "ldap",
                    "check_config": {
                        "credentials": token,
                        "base_dn": "dc=example,dc=org",
                        "search_filter": "(objectClass=person)",
                        "attributes": ["cn"],
                        "expected_output": "competition-service",
                    },
                    "address": "directory.example.org",
                    "port": 636,
                    "timeout": 2,
                }
            )
        self.assertTrue(passed, details)
        self.assertTrue(server.call_args.kwargs["use_ssl"])

    def test_ftps_check_reads_expected_file_without_write_operation(self):
        ftp_instances = []

        class FakeFTP:
            def __init__(self, **_kwargs):
                self.writes = []
                ftp_instances.append(self)

            def connect(self, *_args):
                return None

            def auth(self):
                return None

            def login(self, *_args):
                return None

            def prot_p(self):
                return None

            def retrbinary(self, command, callback, **_kwargs):
                self.command = command
                callback(b"competition is ready")

            def quit(self):
                return None

            def close(self):
                return None

        token = encrypt_credentials("monitor", "ftp-check-password")
        with patch("app.scoring.ftplib.FTP_TLS", FakeFTP):
            passed, details, _latency = _probe(
                {
                    "check_type": "ftp",
                    "check_config": {
                        "credentials": token,
                        "read_filename": "status.txt",
                        "expected_output": "ready",
                    },
                    "address": "127.0.0.1",
                    "port": 21,
                    "timeout": 1,
                }
            )
        self.assertTrue(passed, details)
        self.assertEqual(ftp_instances[0].command, "RETR status.txt")
        self.assertEqual(ftp_instances[0].writes, [])

    def test_ftps_connection_failure_does_not_fail_the_scoring_round(self):
        closed = []

        class FakeFTP:
            def __init__(self, **_kwargs):
                pass

            def connect(self, *_args):
                raise OSError("connection refused")

            def close(self):
                closed.append(True)

        token = encrypt_credentials("monitor", "ftp-check-password")
        competition = Competition(name="FTP connection failure", status="active")
        group = HostGroup(competition=competition, name="Web")
        host = Host(
            competition=competition,
            team=self.team,
            host_group=group,
            address="192.0.2.10",
        )
        service = Service(
            competition=competition,
            host_group=group,
            name="Web FTP",
            check_type="ftp",
            protocol="ftp",
            port=21,
            check_config={
                "credentials": token,
                "read_filename": "status.txt",
                "expected_output": "ready",
            },
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="queued",
        )
        competition.teams.append(self.team)
        db.session.add_all((competition, group, host, service, score_round))
        db.session.commit()

        with patch("app.scoring.ftplib.FTP_TLS", FakeFTP):
            process_scoring_round(score_round.id, self.app)

        db.session.refresh(score_round)
        result = ScoreResult.query.one()
        self.assertEqual(score_round.status, "completed")
        self.assertFalse(result.passed)
        self.assertIn("FTPS read check failed", result.details)
        self.assertEqual(closed, [True])

    def test_ftps_runner_rejects_control_characters_in_legacy_filename(self):
        token = encrypt_credentials("monitor", "ftp-check-password")
        with patch("app.scoring.ftplib.FTP_TLS") as ftp:
            passed, details, _latency = _probe(
                {
                    "check_type": "ftp",
                    "check_config": {
                        "credentials": token,
                        "read_filename": "status.txt\r\nDELE private.txt",
                        "expected_output": "ready",
                    },
                    "address": "127.0.0.1",
                    "port": 21,
                    "timeout": 1,
                }
            )
        self.assertFalse(passed)
        self.assertIn("invalid", details)
        ftp.assert_not_called()

    def test_scoring_round_saves_results_and_credits_team(self):
        server = socket.socket()
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        competition = Competition(
            name="Scoring Cup",
            status="active",
            payout_per_point=Decimal("10.00"),
        )
        competition.teams.append(self.team)
        db.session.add(competition)
        db.session.flush()
        db.session.add(
            Host(
                competition_id=competition.id,
                team_id=self.team.id,
                address="127.0.0.1",
            )
        )
        service = Service(
            competition_id=competition.id,
            name="Web service",
            protocol="tcp",
            port=server.getsockname()[1],
            points=5,
            timeout_seconds=1,
        )
        db.session.add(service)
        db.session.flush()
        score_round = ScoreRound(
            competition_id=competition.id,
            number=1,
            status="queued",
        )
        db.session.add(score_round)
        db.session.commit()

        try:
            process_scoring_round(score_round.id, self.app)
        finally:
            server.close()

        db.session.expire_all()
        score_round = db.session.get(ScoreRound, score_round.id)
        self.assertEqual(score_round.status, "completed")
        result = ScoreResult.query.one()
        self.assertTrue(result.passed)
        self.assertEqual(result.points, 5)
        self.assertEqual(balance_for(self.team.id), Decimal("50.00"))

    def test_scoring_rewards_passing_checks_additively_and_resolves_legacy_tcp(self):
        competition = Competition(
            name="Uptime Reward Cup",
            status="active",
            payout_per_point=Decimal("10.00"),
        )
        competition.teams.append(self.team)
        db.session.add(competition)
        db.session.flush()
        db.session.add(
            Host(
                competition_id=competition.id,
                team_id=self.team.id,
                address="127.0.0.1",
            )
        )
        http_service = Service(
            competition_id=competition.id,
            name="HTTP check",
            check_type="http",
            protocol="http",
            port=80,
            points=2,
        )
        failed_service = Service(
            competition_id=competition.id,
            name="Failed DNS check",
            check_type="dns",
            protocol="dns",
            port=53,
            points=5,
        )
        legacy_tcp_service = Service(
            competition_id=competition.id,
            name="Legacy TCP check",
            protocol="tcp",
            port=443,
            points=0,
        )
        score_round = ScoreRound(
            competition_id=competition.id,
            number=1,
            status="queued",
        )
        db.session.add_all(
            [
                http_service,
                failed_service,
                legacy_tcp_service,
                score_round,
                PlatformSetting(
                    id=1,
                    check_type_payouts={"http": 5, "dns": 11, "tcp": 13},
                ),
            ]
        )
        db.session.commit()

        with patch(
            "app.scoring._probe",
            side_effect=lambda job: (
                job["check_type"] != "dns",
                "Check passed." if job["check_type"] != "dns" else "Check failed.",
                2,
            ),
        ):
            process_scoring_round(score_round.id, self.app)

        db.session.expire_all()
        results = {
            result.service_id: result
            for result in ScoreResult.query.filter_by(round_id=score_round.id).all()
        }
        self.assertTrue(results[http_service.id].passed)
        self.assertEqual(results[http_service.id].points, 2)
        self.assertFalse(results[failed_service.id].passed)
        self.assertTrue(results[legacy_tcp_service.id].passed)

        transactions = {
            transaction.score_result_id: transaction
            for transaction in BankTransaction.query.all()
        }
        self.assertEqual(len(transactions), 2)
        http_transaction = transactions[results[http_service.id].id]
        tcp_transaction = transactions[results[legacy_tcp_service.id].id]
        self.assertEqual(http_transaction.amount, Decimal("25.00"))
        self.assertEqual(tcp_transaction.amount, Decimal("13.00"))
        self.assertNotIn(results[failed_service.id].id, transactions)
        self.assertIn("$5 HTTP / HTTPS uptime reward", http_transaction.description)
        self.assertIn("$13 TCP (legacy) uptime reward", tcp_transaction.description)
        self.assertEqual(balance_for(self.team.id), Decimal("38.00"))

    def test_database_scoring_queue_schedules_and_claims_round_once(self):
        competition = Competition(
            name="Database Queue Cup",
            status="active",
        )
        db.session.add(competition)
        db.session.commit()

        self.assertEqual(schedule_due_rounds(self.app), 1)
        self.assertEqual(schedule_due_rounds(self.app), 0)
        score_round = ScoreRound.query.one()
        self.assertEqual(score_round.status, "queued")
        self.assertEqual(score_round.number, 1)

        claimed_id = claim_next_round(self.app)
        self.assertEqual(claimed_id, score_round.id)
        db.session.refresh(score_round)
        self.assertEqual(score_round.status, "running")
        self.assertIsNotNone(score_round.started_at)
        self.assertIsNone(claim_next_round(self.app))

        with patch("app.tasks.process_scoring_round") as process:
            run_scoring_round(claimed_id, self.app)
        process.assert_called_once_with(claimed_id, self.app, claimed=True)

    def test_scoring_queue_does_not_schedule_while_a_round_is_pending(self):
        competition = Competition(
            name="Single Flight Cup",
            status="active",
        )
        db.session.add(competition)
        db.session.commit()

        self.assertEqual(schedule_due_rounds(self.app), 1)
        competition.next_round_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.session.commit()

        self.assertEqual(schedule_due_rounds(self.app), 0)
        self.assertEqual(ScoreRound.query.filter_by(competition_id=competition.id).count(), 1)

    def test_round_claiming_allows_only_one_running_round_per_competition(self):
        competition = Competition(name="Claim Lock Cup", status="active")
        db.session.add(competition)
        db.session.flush()
        rounds = [
            ScoreRound(competition=competition, number=number, status="queued")
            for number in (1, 2)
        ]
        db.session.add_all(rounds)
        db.session.commit()

        first_claim = claim_next_round(self.app)
        self.assertEqual(first_claim, rounds[0].id)
        self.assertIsNone(claim_next_round(self.app))
        self.assertEqual(
            ScoreRound.query.filter_by(
                competition_id=competition.id,
                status="running",
            ).count(),
            1,
        )

    def test_abandoned_running_rounds_are_marked_failed(self):
        competition = Competition(name="Recover Stale Cup", status="active")
        db.session.add(competition)
        db.session.flush()
        now = datetime.now(timezone.utc)
        stale_round = ScoreRound(
            competition=competition,
            number=1,
            status="running",
            started_at=now - timedelta(minutes=5),
        )
        active_round = ScoreRound(
            competition=competition,
            number=2,
            status="running",
            started_at=now,
            heartbeat_at=now,
        )
        db.session.add_all([stale_round, active_round])
        db.session.commit()

        self.assertEqual(recover_stale_rounds(self.app, stale_after_seconds=90), 1)
        db.session.refresh(stale_round)
        db.session.refresh(active_round)
        self.assertEqual(stale_round.status, "failed")
        self.assertIsNotNone(stale_round.completed_at)
        self.assertIn("stopped reporting progress", stale_round.error_message)
        self.assertEqual(active_round.status, "running")

    def test_scoring_boost_and_hidden_service_still_award_points(self):
        competition = Competition(
            name="Boost Cup",
            status="active",
            payout_per_point=Decimal("10.00"),
        )
        competition.teams.append(self.team)
        db.session.add(competition)
        db.session.flush()
        db.session.add(
            Host(
                competition_id=competition.id,
                team_id=self.team.id,
                address="127.0.0.1",
            )
        )
        service = Service(
            competition_id=competition.id,
            name="Hidden boosted service",
            check_type="http",
            protocol="http",
            port=80,
            points=5,
            points_boost=2,
            hidden_from_scoreboard=True,
        )
        score_round = ScoreRound(
            competition_id=competition.id,
            number=1,
            status="queued",
        )
        db.session.add_all([service, score_round])
        db.session.commit()

        with patch(
            "app.scoring._probe",
            return_value=(True, "Check passed.", 2),
        ) as probe:
            process_scoring_round(score_round.id, self.app)

        probe.assert_called_once()
        result = ScoreResult.query.one()
        self.assertTrue(result.passed)
        self.assertEqual(result.points, 7)
        self.assertTrue(result.service.hidden_from_scoreboard)
        self.assertEqual(balance_for(self.team.id), Decimal("70.00"))
        response = self.client.get(
            f"/api/v1/competitions/{competition.id}/scoreboard"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["standings"][0]["points"], 7)

    def test_scoring_skips_paused_service_without_result_or_credit(self):
        competition = Competition(
            name="Paused Cup",
            status="active",
            payout_per_point=Decimal("10.00"),
        )
        competition.teams.append(self.team)
        db.session.add(competition)
        service = Service(
            competition=competition,
            name="Paused service",
            check_type="dns",
            protocol="dns",
            port=53,
            paused=True,
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="queued",
        )
        db.session.add_all([service, score_round])
        db.session.commit()

        with patch("app.scoring._probe") as probe:
            process_scoring_round(score_round.id, self.app)

        probe.assert_not_called()
        self.assertEqual(ScoreResult.query.count(), 0)
        self.assertEqual(balance_for(self.team.id), Decimal("0.00"))

    def test_scoring_skips_teams_without_accounts(self):
        competition = Competition(
            name="Account-linked scoring",
            status="active",
        )
        unlinked_team = Team(name="Unlinked Team")
        competition.teams.extend((self.team, unlinked_team))
        service = Service(
            competition=competition,
            name="DNS check",
            check_type="dns",
            protocol="dns",
            port=53,
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="queued",
        )
        db.session.add_all((competition, unlinked_team, service, score_round))
        db.session.commit()

        with patch(
            "app.scoring._probe",
            return_value=(True, "Check passed.", None),
        ) as probe:
            process_scoring_round(score_round.id, self.app)

        self.assertEqual(probe.call_count, 1)
        self.assertEqual(
            ScoreResult.query.with_entities(ScoreResult.team_id).all(),
            [(self.team.id,)],
        )

    def test_active_competition_scoreboard_api(self):
        competition = Competition(name="Spring Cup", status="active")
        competition.teams.append(self.team)
        db.session.add(competition)
        db.session.commit()
        response = self.client.get(
            f"/api/v1/competitions/{competition.id}/scoreboard"
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["competition"]["name"], "Spring Cup")

    def test_engine_homepage_defaults_to_scoreboard(self):
        competition = Competition(name="Homepage scoreboard", status="active")
        db.session.add(competition)
        db.session.commit()

        response = self.client.get("/")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'class="scoreboard-page"', response.data)
        self.assertIn(b"<h1>Homepage scoreboard</h1>", response.data)
        self.assertNotIn(b'class="landing-page"', response.data)

    def test_scoreboard_shows_status_matrix_and_zero_score_teams(self):
        competition = Competition(name="Live Board", status="active")
        self.team.name = "Team02"
        second_team = Team(name="Team01")
        second_team_user = User(
            username="team01",
            role="team",
            team=second_team,
        )
        second_team_user.set_password("team01-test-password")
        group = HostGroup(competition=competition, name="Ubuntu")
        ping = Service(
            competition=competition,
            host_group=group,
            name="Ubuntu PING",
            check_type="ping",
            protocol="ping",
            port=0,
            points=2,
        )
        http = Service(
            competition=competition,
            host_group=group,
            name="Ubuntu HTTP",
            check_type="http",
            protocol="http",
            port=80,
            points=3,
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="completed",
        )
        competition.teams.extend((self.team, second_team))
        db.session.add_all(
            (competition, second_team, second_team_user, group, ping, http, score_round)
        )
        db.session.flush()
        db.session.add_all(
            (
                ScoreResult(
                    score_round=score_round,
                    team=self.team,
                    service=ping,
                    passed=True,
                    points=2,
                ),
                ScoreResult(
                    score_round=score_round,
                    team=self.team,
                    service=http,
                    passed=False,
                    points=0,
                    details="HTTP service unavailable.",
                ),
            )
        )
        db.session.commit()

        response = self.client.get("/scoreboard")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<small>HTTP</small>", response.data)
        self.assertIn(b"<small>PING</small>", response.data)
        self.assertIn(b'data-result-status="pass"', response.data)
        self.assertIn(b'data-result-status="fail"', response.data)
        self.assertIn(b'class="scoreboard-page"', response.data)
        self.assertIn(b'class="topbar"', response.data)
        self.assertIn(b'class="footer"', response.data)
        self.assertNotIn(b'>Competition</label>', response.data)
        self.assertIn(b"Team01", response.data)
        self.assertIn(b"Team02", response.data)
        self.assertLess(response.data.index(b"Team01"), response.data.index(b"Team02"))
        self.assertNotIn(b"Rank", response.data)
        self.assertNotIn(b"Points", response.data)
        self.assertNotIn(b'http-equiv="refresh"', response.data)

    def test_scoreboard_live_endpoint_supports_etag_updates(self):
        competition = Competition(name="Live Updates", status="active")
        group = HostGroup(competition=competition, name="Ubuntu")
        service = Service(
            competition=competition,
            host_group=group,
            name="Ubuntu PING",
            check_type="ping",
            protocol="ping",
            port=0,
            points=2,
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="completed",
        )
        competition.teams.append(self.team)
        db.session.add_all((competition, group, service, score_round))
        db.session.flush()
        db.session.add(
            ScoreResult(
                score_round=score_round,
                team=self.team,
                service=service,
                passed=True,
                points=2,
            )
        )
        db.session.commit()

        url = f"/scoreboard/live/{competition.id}"
        response = self.client.get(url)

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json["teams"][0]["score"], 2)
        self.assertTrue(response.json["results"][0]["passed"])
        etag = response.headers["ETag"]
        unchanged = self.client.get(url, headers={"If-None-Match": etag})
        self.assertEqual(unchanged.status_code, 304)

    def test_scoreboards_exclude_unlinked_teams_and_etag_tracks_account_link(self):
        competition = Competition(name="Account-linked teams", status="active")
        unlinked_team = Team(name="Nonexistent Team")
        group = HostGroup(competition=competition, name="Ubuntu")
        service = Service(
            competition=competition,
            host_group=group,
            name="Ubuntu PING",
            check_type="ping",
            protocol="ping",
            port=0,
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="completed",
        )
        competition.teams.extend((self.team, unlinked_team))
        db.session.add_all((competition, group, service, score_round, unlinked_team))
        db.session.flush()
        db.session.add_all(
            (
                ScoreResult(
                    score_round=score_round,
                    team=self.team,
                    service=service,
                    passed=True,
                    points=1,
                ),
                ScoreResult(
                    score_round=score_round,
                    team=unlinked_team,
                    service=service,
                    passed=True,
                    points=1,
                ),
            )
        )
        db.session.commit()

        url = f"/scoreboard/live/{competition.id}"
        initial = self.client.get(url)
        self.assertEqual(initial.status_code, 200)
        self.assertEqual(
            [team["name"] for team in initial.json["teams"]],
            ["Blue Team"],
        )
        etag = initial.headers["ETag"]

        page = self.client.get("/scoreboard")
        self.assertIn(b"Blue Team", page.data)
        self.assertNotIn(b"Nonexistent Team", page.data)
        api_response = self.client.get(
            f"/api/v1/competitions/{competition.id}/scoreboard"
        )
        self.assertEqual(
            [row["team"] for row in api_response.json["standings"]],
            ["Blue Team"],
        )
        self.login("admin", "another-long-test-password")
        for path in (
            "/admin/competitions",
            f"/admin/competitions/{competition.id}",
            f"/admin/competitions/{competition.id}/builder?step=teams",
        ):
            admin_page = self.client.get(path)
            self.assertEqual(admin_page.status_code, 200)
            self.assertNotIn(b"Nonexistent Team", admin_page.data)

        linked_user = User(username="new-team-login", role="team", team=unlinked_team)
        linked_user.set_password("new-team-password")
        db.session.add(linked_user)
        db.session.commit()
        refreshed = self.client.get(url, headers={"If-None-Match": etag})

        self.assertEqual(refreshed.status_code, 200)
        self.assertEqual(
            {team["name"] for team in refreshed.json["teams"]},
            {"Blue Team", "Nonexistent Team"},
        )

    def test_builder_rejects_enrolling_a_team_without_an_account(self):
        competition = Competition(name="Enrollment guard")
        unlinked_team = Team(name="Unlinked Team")
        db.session.add_all((competition, unlinked_team))
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=teams",
            data={
                "action": "enroll-team",
                "step": "teams",
                "team_id": str(unlinked_team.id),
            },
        )

        self.assertEqual(response.status_code, 302)
        self.assertNotIn(unlinked_team, competition.teams)

    def test_scoreboard_competition_selector_is_admin_controlled(self):
        first = Competition(name="A Default Board", status="active")
        second = Competition(name="B Optional Board", status="active")
        db.session.add_all((first, second))
        db.session.commit()

        response = self.client.get(f"/scoreboard?competition={second.id}")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<h1>A Default Board</h1>", response.data)
        self.assertNotIn(b'name="competition"', response.data)

        self.login("admin", "another-long-test-password")
        response = self.client.post(
            "/admin/management-controls",
            data={
                "scoreboard_competition_selector_enabled": "on",
                "score_check_hints_enabled": "on",
            },
        )
        self.assertEqual(response.status_code, 302)
        settings = db.session.get(PlatformSetting, 1)
        self.assertTrue(settings.scoreboard_competition_selector_enabled)
        self.assertTrue(settings.score_check_hints_enabled)
        response = self.client.get(f"/scoreboard?competition={second.id}")
        self.assertIn(b"<h1>B Optional Board</h1>", response.data)
        self.assertIn(b'name="competition"', response.data)

        self.client.post("/admin/management-controls", data={})
        settings = db.session.get(PlatformSetting, 1)
        self.assertFalse(settings.scoreboard_competition_selector_enabled)
        self.assertFalse(settings.score_check_hints_enabled)

    def test_archived_scoreboard_is_only_listed_in_management_controls(self):
        competition = Competition(
            name="Archived board",
            status="active",
            next_round_at=None,
        )
        competition.teams.append(self.team)
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="queued",
        )
        db.session.add_all([competition, score_round])
        db.session.commit()
        competition_id = competition.id
        round_id = score_round.id

        self.login("admin", "another-long-test-password")
        response = self.client.post(
            f"/admin/competitions/{competition_id}/archive"
        )

        self.assertEqual(response.status_code, 302)
        archived = db.session.get(Competition, competition_id)
        self.assertTrue(archived.is_archived)
        self.assertEqual(archived.status, "paused")
        self.assertEqual(archived.archived_status, "active")
        competitions_page = self.client.get("/admin/competitions").data
        scoreboard_rows = competitions_page.split(b"<tbody>")[-1].split(
            b"</tbody>",
            1,
        )[0]
        self.assertNotIn(b"Archived board", scoreboard_rows)
        self.assertNotIn(b"Archived board", self.client.get("/admin/").data)
        self.assertNotIn(b"Archived board", self.client.get("/scoreboard").data)
        self.assertNotIn(
            competition_id,
            [
                board["id"]
                for board in self.client.get("/api/v1/competitions").json["competitions"]
            ],
        )
        self.assertEqual(
            self.client.get(f"/scoreboard/live/{competition_id}").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/api/v1/competitions/{competition_id}/scoreboard"
            ).status_code,
            404,
        )
        self.assertIn(
            b"Archived board",
            self.client.get("/admin/management-controls").data,
        )
        self.assertEqual(claim_next_round(self.app), None)

        response = self.client.post(
            f"/admin/competitions/{competition_id}/unarchive"
        )

        self.assertEqual(response.status_code, 302)
        restored = db.session.get(Competition, competition_id)
        self.assertFalse(restored.is_archived)
        self.assertEqual(restored.status, "active")
        self.assertIsNotNone(restored.next_round_at)
        self.assertEqual(claim_next_round(self.app), round_id)

    def test_team_check_credentials_are_scoped_and_saved_encrypted(self):
        competition = Competition(name="Team Credentials", status="active")
        own_group = HostGroup(competition=competition, name="Ubuntu")
        other_group = HostGroup(competition=competition, name="Windows")
        service = Service(
            competition=competition,
            host_group=own_group,
            name="Ubuntu SSH",
            check_type="ssh",
            check_config={"credentials": encrypt_credentials("service-user", "base-password")},
            protocol="ssh",
            port=22,
        )
        other_service = Service(
            competition=competition,
            host_group=other_group,
            name="Windows WinRM",
            check_type="winrm",
            check_config={"credentials": encrypt_credentials("windows-user", "windows-password")},
            protocol="winrm",
            port=5986,
        )
        other_team = Team(name="Red Team")
        other_team_user = User(
            username="credential-red-team",
            role="team",
            team=other_team,
        )
        other_team_user.set_password("red-team-test-password")
        own_host = Host(
            competition=competition,
            team=self.team,
            host_group=own_group,
            address="192.0.2.10",
        )
        other_host = Host(
            competition=competition,
            team=other_team,
            host_group=other_group,
            address="192.0.2.11",
        )
        competition.teams.extend((self.team, other_team))
        db.session.add_all(
            (
                competition,
                own_group,
                other_group,
                service,
                other_service,
                other_team,
                other_team_user,
                own_host,
                other_host,
            )
        )
        db.session.commit()
        self.login("blue", "a-long-test-password")

        response = self.client.get(
            f"/team/checks?competition={competition.id}&team={other_team.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Ubuntu SSH", response.data)
        self.assertNotIn(b"Windows WinRM", response.data)
        self.assertIn(b"Username is managed by organizers", response.data)
        self.assertIn(b'type="password"', response.data)
        self.assertNotIn(b"base-password", response.data)

        response = self.client.post(
            "/team/checks",
            data={
                "competition": str(competition.id),
                "team": str(other_team.id),
                "service_id": str(service.id),
                "password": "team-password",
            },
        )
        self.assertEqual(response.status_code, 302)
        own_override = TeamServiceConfig.query.filter_by(
            team_id=self.team.id,
            service_id=service.id,
        ).one()
        self.assertNotIn("team-password", own_override.credentials_override)
        self.assertEqual(
            decrypt_credentials(own_override.credentials_override),
            ("service-user", "team-password"),
        )
        self.assertTrue(self.team_user.check_password("a-long-test-password"))
        self.assertFalse(self.team_user.check_password("team-password"))
        self.assertIsNone(
            TeamServiceConfig.query.filter_by(
                team_id=other_team.id,
                service_id=service.id,
            ).first()
        )
        response = self.client.post(
            "/team/checks",
            data={
                "competition": str(competition.id),
                "team": str(other_team.id),
                "service_id": str(other_service.id),
                "password": "forbidden-password",
            },
        )
        self.assertEqual(response.status_code, 404)
        self.assertIsNone(
            TeamServiceConfig.query.filter_by(
                team_id=self.team.id,
                service_id=other_service.id,
            ).first()
        )

        banker = User(username="banker", role="banker")
        banker.set_password("banker-long-test-password")
        db.session.add(banker)
        db.session.commit()
        self.client.post("/logout")
        login_response = self.login("banker", "banker-long-test-password")
        self.assertEqual(login_response.status_code, 302)
        response = self.client.get("/team/checks")
        self.assertEqual(response.status_code, 403)
        self.client.post("/logout")
        self.login("admin", "another-long-test-password")
        response = self.client.get(
            f"/team/checks?competition={competition.id}&team={other_team.id}"
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"Windows WinRM", response.data)
        self.assertNotIn(b"Ubuntu SSH", response.data)

    def test_team_checks_show_failure_details_and_optional_hints(self):
        competition = Competition(name="Team check details", status="active")
        group = HostGroup(competition=competition, name="Ubuntu")
        service = Service(
            competition=competition,
            host_group=group,
            name="Ubuntu SSH",
            check_type="ssh",
            check_config={
                "credentials": encrypt_credentials("monitor", "check-secret"),
            },
            protocol="ssh",
            port=22,
            points=3,
        )
        read_only_service = Service(
            competition=competition,
            host_group=group,
            name="Ubuntu Ping",
            check_type="ping",
            protocol="icmp",
            port=0,
        )
        host = Host(
            competition=competition,
            team=self.team,
            host_group=group,
            address="192.0.2.10",
        )
        score_round = ScoreRound(
            competition=competition,
            number=1,
            status="completed",
        )
        competition.teams.append(self.team)
        db.session.add_all(
            (competition, group, service, read_only_service, host, score_round)
        )
        db.session.flush()
        db.session.add(
            ScoreResult(
                score_round=score_round,
                team=self.team,
                service=service,
                passed=False,
                points=0,
                details="SSH authentication was rejected by the server.",
            )
        )
        db.session.commit()

        self.login("blue", "a-long-test-password")
        response = self.client.get(f"/team/checks?competition={competition.id}")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"SSH authentication was rejected by the server.", response.data)
        self.assertIn(b"192.0.2.10", response.data)
        self.assertIn(b"SSH", response.data)
        self.assertIn(b"22", response.data)
        self.assertIn(b"Latest result", response.data)
        self.assertIn(b"Check properties", response.data)
        self.assertIn(b"Scored username", response.data)
        self.assertIn(b"monitor", response.data)
        self.assertIn(b'class="team-check-panels"', response.data)
        self.assertIn(b"team-check-credential-empty", response.data)
        self.assertNotIn(b"UNLOCKED CREDENTIALS", response.data)
        self.assertNotIn(b"<details", response.data)
        self.assertNotIn(
            b"This changes the password used by this check, not your UBNETDEF account password.",
            response.data,
        )
        self.assertNotIn(b"check-secret", response.data)
        self.assertNotIn(b"Suggested next step", response.data)

        self.client.post("/logout")
        self.login("admin", "another-long-test-password")
        response = self.client.post(
            "/admin/management-controls",
            data={"score_check_hints_enabled": "on"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertTrue(db.session.get(PlatformSetting, 1).score_check_hints_enabled)

        self.client.post("/logout")
        self.login("blue", "a-long-test-password")
        response = self.client.get(f"/team/checks?competition={competition.id}")

        self.assertIn(b"Suggested next step", response.data)
        self.assertIn(b"Review the account and authentication method", response.data)

    def test_team_check_competition_picker_requires_multiple_active_competitions(self):
        first = Competition(name="One Active Board", status="active")
        first.teams.append(self.team)
        db.session.add(first)
        db.session.commit()

        self.login("blue", "a-long-test-password")
        response = self.client.get("/team/checks")

        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b'<label for="team-check-competition">Competition</label>', response.data)
        self.assertNotIn(b"One Active Board", response.data)

        second = Competition(name="Another Active Board", status="active")
        second.teams.append(self.team)
        db.session.add(second)
        db.session.commit()
        response = self.client.get("/team/checks")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b'<label for="team-check-competition">Competition</label>', response.data)
        self.assertIn(b"One Active Board", response.data)
        self.assertIn(b"Another Active Board", response.data)

    def test_admin_team_picker_is_aligned_with_team_checks_heading(self):
        competition = Competition(name="Admin Check Board", status="active")
        competition.teams.append(self.team)
        db.session.add(competition)
        db.session.commit()

        self.login("admin", "another-long-test-password")
        response = self.client.get("/team/checks")

        self.assertEqual(response.status_code, 200)
        self.assertIn(b"<h1>Team Checks</h1>", response.data)
        self.assertIn(b'id="team-check-team"', response.data)
        self.assertIn(
            f'name="competition" value="{competition.id}"'.encode(),
            response.data,
        )
        heading_start = response.data.index(
            b'<section class="page-heading page-heading-row">'
        )
        heading_end = response.data.index(b"</section>", heading_start)
        self.assertLess(
            response.data.index(b"team-check-team-picker"),
            heading_end,
        )

    def test_failure_hints_follow_error_details(self):
        examples = (
            (
                "ssh",
                "SSH authentication was rejected by the server.",
                "Review the account and authentication method used by this check.",
            ),
            (
                "ldap",
                "LDAP connection failed: socket connection error while opening: timed out",
                "Check whether the service is reachable and can respond within its configured timeout.",
            ),
            (
                "http",
                "Expected HTTP 200; received HTTP 503.",
                "Compare the check's expected response with what the service currently returns.",
            ),
            (
                "ftp",
                "FTPS TLS certificate verification failed.",
                "Review the encrypted connection and certificate settings used by this check.",
            ),
        )
        for check_type, details, expected_hint in examples:
            with self.subTest(check_type=check_type, details=details):
                self.assertEqual(
                    _recommended_score_check_action(check_type, details),
                    expected_hint,
                )

    def test_scoring_uses_team_password_overrides(self):
        competition = Competition(name="Team Credential Scoring", status="active")
        group = HostGroup(competition=competition, name="Ubuntu")
        other_team = Team(name="Red Team")
        other_team_user = User(
            username="credential-scoring-red-team",
            role="team",
            team=other_team,
        )
        other_team_user.set_password("red-team-test-password")
        service = Service(
            competition=competition,
            host_group=group,
            name="Ubuntu SSH",
            check_type="ssh",
            check_config={"credentials": encrypt_credentials("service-user", "base-password")},
            protocol="ssh",
            port=22,
        )
        hosts = (
            Host(competition=competition, team=self.team, host_group=group, address="192.0.2.10"),
            Host(competition=competition, team=other_team, host_group=group, address="192.0.2.11"),
        )
        configuration = TeamServiceConfig(
            team=self.team,
            service=service,
            credentials_override=encrypt_credentials("ignored-team-user", "team-password"),
        )
        score_round = ScoreRound(competition=competition, number=1, status="queued")
        competition.teams.extend((self.team, other_team))
        db.session.add_all(
            (
                competition,
                group,
                other_team,
                other_team_user,
                service,
                *hosts,
                configuration,
                score_round,
            )
        )
        db.session.commit()
        seen_jobs = []

        def fake_probe(job):
            seen_jobs.append(job)
            return True, "Check passed.", 1

        with patch("app.scoring._probe", side_effect=fake_probe):
            process_scoring_round(score_round.id, self.app)

        credentials_by_team = {
            job["team_id"]: _credentials(job["check_config"]) for job in seen_jobs
        }
        self.assertEqual(
            credentials_by_team[self.team.id],
            ("service-user", "team-password"),
        )
        self.assertEqual(
            credentials_by_team[other_team.id],
            ("service-user", "base-password"),
        )

    def test_team_host_address_updates_are_scoped_and_range_limited(self):
        competition = Competition(name="Host Access Cup", status="active")
        group = HostGroup(competition=competition, name="Ubuntu")
        host = Host(
            competition=competition,
            team=self.team,
            host_group=group,
            address="192.0.2.10",
            team_can_edit_address=True,
            allowed_address_ranges="192.0.2.0/24",
        )
        other_team = Team(name="Red Team")
        other_host = Host(
            competition=competition,
            team=other_team,
            host_group=group,
            address="192.0.2.11",
            team_can_edit_address=True,
            allowed_address_ranges="192.0.2.0/24",
        )
        competition.teams.extend((self.team, other_team))
        db.session.add_all((competition, group, host, other_team, other_host))
        db.session.commit()
        self.login("blue", "a-long-test-password")

        response = self.client.post(
            f"/scoreboard/hosts/{host.id}",
            data={"address": "192.0.2.20"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(db.session.get(Host, host.id).address, "192.0.2.20")

        response = self.client.post(
            f"/scoreboard/hosts/{host.id}",
            data={"address": "198.51.100.10"},
            follow_redirects=True,
        )
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"allowed range", response.data)
        self.assertEqual(db.session.get(Host, host.id).address, "192.0.2.20")

        self.client.post(
            f"/scoreboard/hosts/{other_host.id}",
            data={"address": "192.0.2.21"},
        )
        self.assertEqual(db.session.get(Host, other_host.id).address, "192.0.2.11")
        response = self.client.get("/team/checks")
        self.assertIn(b"Device addresses", response.data)
        self.assertIn(b"192.0.2.20", response.data)
        self.assertNotIn(b"192.0.2.11", response.data)

    def test_admin_can_configure_team_host_address_permissions(self):
        competition = Competition(name="Host Permissions")
        group = HostGroup(competition=competition, name="Ubuntu")
        host = Host(
            competition=competition,
            team=self.team,
            host_group=group,
            address="192.0.2.10",
        )
        competition.teams.append(self.team)
        db.session.add_all((competition, group, host))
        db.session.commit()
        self.login("admin", "another-long-test-password")

        response = self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=teams",
            data={
                "action": "save-host-address",
                "step": "teams",
                "host_id": str(host.id),
                "address": "192.0.2.10",
                "team_can_edit_address": "on",
                "allowed_address_ranges": "192.0.2.0/24, host.example.org",
            },
        )

        self.assertEqual(response.status_code, 302)
        saved = db.session.get(Host, host.id)
        self.assertTrue(saved.team_can_edit_address)
        self.assertEqual(
            saved.allowed_address_ranges,
            "192.0.2.0/24,host.example.org",
        )

        self.client.post(
            f"/admin/competitions/{competition.id}/builder?step=teams",
            data={
                "action": "save-host-address",
                "step": "teams",
                "host_id": str(host.id),
                "address": "192.0.2.10",
                "team_can_edit_address": "on",
                "allowed_address_ranges": "",
            },
        )
        self.assertTrue(db.session.get(Host, host.id).team_can_edit_address)
        self.assertEqual(
            db.session.get(Host, host.id).allowed_address_ranges,
            "192.0.2.0/24,host.example.org",
        )

    def test_staff_pages_render(self):
        competition = Competition(name="Render Cup")
        db.session.add(competition)
        db.session.commit()
        self.login("admin", "another-long-test-password")
        paths = [
            "/",
            "/scoreboard",
            "/admin/",
            "/admin/users",
            "/admin/competitions",
            f"/admin/competitions/{competition.id}",
            "/bank/",
            "/store/",
            "/store/manage/orders",
            "/store/manage/products",
        ]
        for path in paths:
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200)

    def test_team_pages_render(self):
        db.session.add(
            BankTransaction(
                team_id=self.team.id,
                kind="credit",
                amount=Decimal("3.00"),
                description="Test award",
            )
        )
        db.session.commit()
        self.login("blue", "a-long-test-password")
        for path in ["/bank/account", "/store/", "/store/orders"]:
            with self.subTest(path=path):
                response = self.client.get(path, follow_redirects=True)
                self.assertEqual(response.status_code, 200)

    def test_preview_links_stay_on_preview_host(self):
        response = self.client.get("/")
        self.assertEqual(response.status_code, 200)
        self.assertNotIn(b"https://engine.example.org", response.data)

    def test_public_site_links_use_configured_subdomains(self):
        response = self.client.get("/", base_url="https://engine.example.org")
        self.assertEqual(response.status_code, 200)
        self.assertIn(b"https://bank.example.org/bank", response.data)


if __name__ == "__main__":
    unittest.main()
