"""Run the app's regression tests on a disposable, test-enabled Frappe site."""

import importlib
import unittest
from unittest.mock import patch

import frappe
from frappe.tests.utils import toggle_test_mode


def execute():
	if not frappe.conf.allow_tests:
		raise RuntimeError("DHL tests require allow_tests on a disposable test site")

	modules = [
		"test_multi_parcel_barcode",
		"test_createbarcode_response",
		"test_sync_barcode_rows",
		"test_label_pdf_cleanup",
		"test_v16_compatibility",
		"test_v16_schema",
	]
	suite = unittest.TestSuite()
	previous_test_mode = frappe.in_test
	toggle_test_mode(True)
	try:
		for name in modules:
			module = importlib.import_module(f"dhl_ecommerce_integration.tests.{name}")
			suite.addTests(unittest.defaultTestLoader.loadTestsFromModule(module))
			# Upstream tests use plain functions; unittest/bench otherwise skip them.
			for name in sorted(vars(module)):
				function = getattr(module, name)
				if name.startswith("test_") and callable(function):
					suite.addTest(unittest.FunctionTestCase(function))

		with patch(
			"requests.sessions.Session.request",
			side_effect=AssertionError("Live HTTP is forbidden in DHL tests"),
		):
			result = unittest.TextTestRunner(verbosity=2).run(suite)
		if not result.wasSuccessful():
			raise AssertionError(
				f"DHL tests failed: {len(result.failures)} failures, {len(result.errors)} errors"
			)
		return {"total": result.testsRun, "passed": result.testsRun, "failed": 0}
	finally:
		toggle_test_mode(previous_test_mode)
