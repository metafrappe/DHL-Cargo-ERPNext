"""Database-backed checks; run only in the disposable CI site's test runner."""

import io
import unittest
from unittest.mock import MagicMock, patch

import frappe
from pypdf import PdfReader, PdfWriter

from dhl_ecommerce_integration import install, tasks, utils


class TestV16Schema(unittest.TestCase):
	def setUp(self):
		if not frappe.conf.allow_tests:
			raise RuntimeError("A disposable site with allow_tests is required")
		self.http = self.enterContext(
			patch("requests.sessions.Session.request", side_effect=AssertionError("Live HTTP is forbidden"))
		)
		self.enterContext(patch.object(frappe, "log_error"))
		self.savepoint = "dhl_v16_" + frappe.generate_hash(length=10)
		frappe.db.savepoint(self.savepoint)
		self.addCleanup(lambda: frappe.db.rollback(save_point=self.savepoint))

	def test_installed_fields_and_seed_records(self):
		# These exist only if install/migrate successfully ran the app hooks.
		for doctype, fields in install.get_custom_fields().items():
			meta = frappe.get_meta(doctype)
			for field in fields:
				self.assertIsNotNone(meta.get_field(field["fieldname"]))
		field = frappe.get_meta("Delivery Note").get_field("custom_ld_delivery_method")
		self.assertEqual(field.fieldtype, "Select")
		self.assertEqual(field.options.splitlines().count("DHL"), 1)
		for doctype, rows in (
			("DHL Delivery Type", install.DELIVERY_TYPES),
			("DHL Payment Type", install.PAYMENT_TYPES),
		):
			for row in rows:
				self.assertEqual(frappe.db.count(doctype, {"code": row["code"]}), 1)
		self.http.assert_not_called()

	def test_settings_credentials_are_not_readable_by_all_users(self):
		meta = frappe.get_meta("DHL Cargo Settings")
		self.assertFalse(any(row.role == "All" and row.read for row in meta.permissions))

	def test_tracking_query_uses_real_v16_database(self):
		# Exercise v16 query-builder filters and pluck, not mocked frappe.get_all.
		self.assertIsInstance(tasks._get_active_delivery_notes(), list)
		self.assertIsInstance(tasks._get_active_return_orders(), list)
		self.assertTrue(utils._delete_stale_label_files("DHL-V16-NONEXISTENT").op_result)
		self.http.assert_not_called()

	def _delivery_note(self):
		# Direct test insertion avoids ERPNext inventory/GL side effects; the real
		# DeliveryNote controller, schema, child table and SQL storage still run.
		doc = frappe.new_doc("Delivery Note")
		doc.name = "DHL-V16-" + frappe.generate_hash(length=8)
		doc.custom_ld_delivery_method = "DHL"
		doc.shipping_address_name = "DHL-V16-ADDRESS"
		doc.customer_name = "Test Recipient"
		doc.docstatus = 1
		doc.append("items", {"item_code": "DHL-V16-ITEM", "item_group": "Products", "qty": 1})
		doc.append("dhl_barcodes", {"piece_number": 1, "desi": 2, "kg": 3})
		doc.db_insert()
		for child in doc.get_all_children():
			child.db_insert()
		return doc

	def test_create_order_uses_real_delivery_note_controller(self):
		doc = self._delivery_note()
		settings = frappe.get_doc(
			{
				"doctype": "DHL Cargo Settings",
				"enabled": 1,
				"web_service_url": "https://carrier.invalid",
				"client_id": "test",
				"cities": [],
				"districts": [],
			}
		)
		address = frappe.get_doc(
			{
				"doctype": "Address",
				"city": "istanbul",
				"county": "şişli",
				"address_line1": "Test street",
				"phone": "+90 555 123 45 67",
			}
		)
		response = MagicMock(status_code=200)
		response.json.return_value = [
			{"referenceId": doc.name, "orderInvoiceId": "TEST-INVOICE", "shipperBranchCode": "TEST-BRANCH"}
		]
		original_get_doc = frappe.get_doc

		def get_doc(*args, **kwargs):
			if args == ("Address", doc.shipping_address_name):
				return address
			return original_get_doc(*args, **kwargs)

		with (
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(frappe, "get_doc", side_effect=get_doc),
			patch.object(settings, "get_password", return_value="test-secret"),
			patch.object(utils, "_get_token", return_value=frappe._dict(op_result=True, token="test-token")),
			patch.object(utils.requests, "post", return_value=response) as post,
		):
			self.assertTrue(utils._create_order_on_submit(doc).op_result)
		payload = post.call_args.kwargs["json"]
		self.assertEqual(payload["recipient"]["mobilePhoneNumber"], "5551234567")
		self.assertEqual(payload["recipient"]["districtName"], "ŞİŞLİ")
		self.assertEqual(payload["orderPieceList"][0]["desi"], 2)
		self.assertEqual(frappe.db.get_value("Delivery Note", doc.name, "dhl_reference_id"), doc.name)
		self.assertEqual(post.call_count, 1)
		self.http.assert_not_called()

	def test_barcode_storage_and_pdf_attachment_use_real_schema(self):
		doc = self._delivery_note()
		settings = frappe.get_doc(
			{
				"doctype": "DHL Cargo Settings",
				"enabled": 1,
				"web_service_url": "https://carrier.invalid",
				"client_id": "test",
			}
		)
		frappe.db.set_value("Delivery Note", doc.name, "dhl_reference_id", doc.name)
		response = MagicMock(status_code=200)
		response.json.return_value = [
			{
				"referenceId": doc.name,
				"invoiceId": "TEST-INVOICE",
				"shipmentId": "TEST-SHIPMENT",
				"barcodes": [
					{"pieceNumber": 1, "value": "^XA test1 ^XZ"},
					{"pieceNumber": 2, "value": "^XA test2 ^XZ"},
				],
			}
		]
		writer = PdfWriter()
		writer.add_blank_page(width=288, height=288)
		buffer = io.BytesIO()
		writer.write(buffer)
		with (
			patch.object(frappe, "get_single", return_value=settings),
			patch.object(settings, "get_password", return_value="test-secret"),
			patch.object(utils, "_get_token", return_value=frappe._dict(op_result=True, token="test-token")),
			patch.object(utils.requests, "post", return_value=response) as post,
			patch.object(utils, "_convert_zpl_to_pdf", return_value=buffer.getvalue()),
		):
			result = utils.create_barcode(doc.name, [{"desi": 2, "kg": 3}, {"desi": 4, "kg": 5}])
		self.assertTrue(result.op_result)
		self.assertEqual(post.call_count, 1)
		doc.reload()
		self.assertEqual([row.idx for row in doc.dhl_barcodes], [1, 2])
		self.assertEqual([row.barcode for row in doc.dhl_barcodes], [doc.name + "-01", doc.name + "-02"])
		self.assertEqual(len(result.pdf_urls), 3)
		files = frappe.get_all(
			"File",
			filters={"attached_to_doctype": "Delivery Note", "attached_to_name": doc.name},
			pluck="name",
		)
		self.assertEqual(len(files), 3)
		try:
			combined = frappe.get_doc("File", {"file_url": result.pdf_urls[-1]})
			self.assertTrue(combined.is_private)
			# File.get_content() decodes UTF-8-compatible files to str. Read the
			# stored PDF in binary mode so the assertion checks its original bytes.
			with open(combined.get_full_path(), "rb") as pdf_file:
				self.assertEqual(len(PdfReader(pdf_file).pages), 2)
		finally:
			for name in files:
				frappe.delete_doc("File", name, ignore_permissions=True)
		self.http.assert_not_called()

	def test_recipient_accepts_standard_customer_without_custom_tax_office(self):
		customer = frappe.get_doc({"doctype": "Customer", "customer_name": "Test", "tax_id": "1234567890"})
		self.assertIsNone(customer.get("custom_tax_office"))
		settings = frappe.get_doc(
			{
				"doctype": "DHL Cargo Settings",
				"web_service_url": "https://carrier.invalid",
				"client_id": "test",
			}
		)
		address = frappe.get_doc(
			{"doctype": "Address", "city": "İSTANBUL", "county": "ŞİŞLİ", "address_line1": "Test street"}
		)
		response = MagicMock(status_code=200)
		response.json.return_value = {"customerId": "TEST-CUSTOMER"}
		with (
			patch.object(frappe, "get_doc", return_value=customer),
			patch.object(customer, "save"),
			patch.object(settings, "get_password", return_value="test-secret"),
			patch.object(utils.requests, "post", return_value=response) as post,
		):
			utils._send_create_recipient(
				frappe._dict(customer="TEST", customer_name="Test", name="SO-TEST"),
				settings,
				address,
				"34",
				"343",
				"test-token",
			)
		self.assertEqual(post.call_args.kwargs["json"]["recipient"]["taxOffice"], "")
		self.http.assert_not_called()
