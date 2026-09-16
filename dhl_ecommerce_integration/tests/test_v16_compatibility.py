"""Isolated regression tests using the real Frappe v16 module and mocked I/O."""

import base64
import json
import unittest
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import frappe

from dhl_ecommerce_integration import install, tasks, utils


class TestV16Compatibility(unittest.TestCase):
	def setUp(self):
		self.http = self.enterContext(
			patch("requests.sessions.Session.request", side_effect=AssertionError("Unexpected external HTTP"))
		)

	def test_missing_legacy_delivery_method_is_a_noop(self):
		doc = MagicMock()
		doc.get.return_value = None
		with patch.object(utils, "_create_order_on_submit") as create:
			utils.on_submit_delivery_note(doc, "on_submit")
		create.assert_not_called()
		self.http.assert_not_called()

	def test_disabled_settings_leave_business_hooks_untouched(self):
		settings = frappe._dict(enabled=0)
		doc = MagicMock()
		doc.get.return_value = "DHL"
		with (
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(frappe, "db", MagicMock()) as db,
		):
			db.get_single_value.return_value = 0
			utils.on_submit_delivery_note(doc, "on_submit")
			utils.create_recipient(doc, "on_submit")
			self.assertTrue(utils.validate_address(doc, "validate").op_result)
			self.assertEqual(tasks.dhl_hourly_tracking().processed, 0)
			self.assertEqual(tasks.dhl_hourly_return_tracking().processed, 0)
		doc.add_comment.assert_not_called()
		self.http.assert_not_called()

	def test_unconfigured_token_does_not_contact_carrier(self):
		with patch.object(frappe, "get_single", return_value=frappe._dict()):
			self.assertFalse(utils._get_token().op_result)
		self.http.assert_not_called()

	def test_missing_password_does_not_contact_carrier(self):
		settings = MagicMock(jwt_token="", jwt_expire_date="")
		settings.get.return_value = "configured"
		settings.get_password.return_value = None
		with patch.object(frappe, "get_single", return_value=settings):
			self.assertFalse(utils._get_token().op_result)
		self.http.assert_not_called()

	def test_token_cache_accepts_datetime_field(self):
		now = datetime(2026, 9, 16, 12)
		settings = frappe._dict(jwt_token="opaque-cached-token", jwt_expire_date=now + timedelta(hours=1))
		with (
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(utils, "now_datetime", return_value=now),
		):
			self.assertEqual(utils._get_token().token, "opaque-cached-token")
		self.http.assert_not_called()

	def test_jwt_refresh_margin_is_before_expiry(self):
		def jwt(seconds):
			payload = json.dumps(
				{"exp": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).timestamp()}
			)
			return "header." + base64.urlsafe_b64encode(payload.encode()).decode().rstrip("=") + ".signature"

		self.assertFalse(utils._is_jwt_exp_valid(jwt(-30)))
		self.assertFalse(utils._is_jwt_exp_valid(jwt(120)))
		self.assertTrue(utils._is_jwt_exp_valid(jwt(3600)))

	def test_token_endpoint_requires_system_manager(self):
		with (
			patch.object(frappe, "only_for", side_effect=frappe.PermissionError),
			patch.object(utils, "_get_token") as token,
		):
			with self.assertRaises(frappe.PermissionError):
				utils.get_token()
		token.assert_not_called()

	def test_city_refresh_requires_system_manager(self):
		with (
			patch.object(frappe, "only_for", side_effect=frappe.PermissionError),
			patch.object(frappe, "enqueue") as enqueue,
		):
			with self.assertRaises(frappe.PermissionError):
				utils.get_cities_and_districts()
		enqueue.assert_not_called()

	def test_barcode_endpoint_checks_document_permission_before_api(self):
		doc = MagicMock()
		doc.check_permission.side_effect = frappe.PermissionError
		with (
			patch.object(frappe, "get_single", return_value=frappe._dict(enabled=1)),
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(utils, "_get_token") as token,
		):
			with self.assertRaises(frappe.PermissionError):
				utils.create_barcode("DENIED", [{"desi": 1, "kg": 1}])
		doc.check_permission.assert_called_once_with("write")
		token.assert_not_called()

	def test_pdf_endpoint_checks_document_permission_before_api(self):
		doc = MagicMock()
		doc.check_permission.side_effect = frappe.PermissionError
		with (
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(utils, "_generate_pdfs_for_dn") as generate,
		):
			with self.assertRaises(frappe.PermissionError):
				utils.generate_dhl_pdfs("DENIED")
		generate.assert_not_called()

	def test_cancel_endpoint_checks_permission_before_api(self):
		doc = MagicMock()
		doc.check_permission.side_effect = frappe.PermissionError
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "get_doc", return_value=doc),
			patch.object(utils, "_get_token") as token,
		):
			db.get_value.return_value = "DENIED"
			with self.assertRaises(frappe.PermissionError):
				utils.cancel_dhl_order("REF-DENIED")
		token.assert_not_called()

	def test_request_logs_redact_nested_credentials(self):
		with patch.object(frappe, "log_error") as log:
			utils._log_api_request(
				frappe._dict(enable_detailed_logs=1),
				"test",
				"POST",
				"https://carrier.invalid/token",
				{"authorization": "secret-bearer", "x-ibm-client-secret": "secret-client"},
				{
					"password": "secret-password",
					"nested": [{"jwt": "secret-jwt", "refreshToken": "secret-refresh"}],
				},
			)
		serialized = log.call_args.args[1]
		for secret in ("secret-bearer", "secret-client", "secret-password", "secret-jwt", "secret-refresh"):
			self.assertNotIn(secret, serialized)

	def test_token_response_logs_redact_tokens(self):
		settings = MagicMock(
			jwt_token="",
			jwt_expire_date="",
			enable_detailed_logs=1,
			web_service_url="https://carrier.invalid",
		)
		settings.get.return_value = "configured"
		settings.customer_number = "test-account"
		settings.client_id = "test-client"
		settings.get_password.return_value = "secret-password"
		response = MagicMock(status_code=200)
		response.json.return_value = {"jwt": "secret-jwt", "refreshToken": "secret-refresh"}
		with (
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(utils.requests, "post", return_value=response),
			patch.object(frappe, "log_error") as log,
		):
			self.assertTrue(utils._get_token().op_result)
		for call in log.call_args_list:
			for secret in ("secret-password", "secret-jwt", "secret-refresh"):
				self.assertNotIn(secret, call.args[1])

	def test_install_preserves_existing_delivery_choices(self):
		field = SimpleNamespace(options="\nCourier\nPickup", save=MagicMock())
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "get_doc", return_value=field),
			patch.object(frappe, "clear_cache"),
		):
			db.exists.return_value = True
			db.get_value.return_value = "Delivery Note-custom_ld_delivery_method"
			install.ensure_delivery_method_has_dhl()
			install.ensure_delivery_method_has_dhl()
		self.assertEqual(field.options, "\nCourier\nPickup\nDHL")
		field.save.assert_called_once_with(ignore_permissions=True)

	def test_install_creates_missing_delivery_selector(self):
		with (
			patch.object(frappe, "db", MagicMock()) as db,
			patch.object(frappe, "get_meta", return_value=MagicMock()) as meta,
			patch.object(install, "create_custom_fields") as create,
		):
			db.exists.return_value = True
			db.get_value.return_value = None
			meta.return_value.get_field.return_value = None
			install.ensure_delivery_method_has_dhl()
		field = create.call_args.args[0]["Delivery Note"][0]
		self.assertEqual(field["fieldname"], "custom_ld_delivery_method")
		self.assertEqual(field["options"], "\nDHL")
