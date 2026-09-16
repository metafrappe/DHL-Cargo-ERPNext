"""Shared-site CI entry point. Never invokes a live carrier operation."""

from dhl_ecommerce_integration.run_all_tests import execute


def run(app="dhl_ecommerce_integration"):
	if app != "dhl_ecommerce_integration":
		raise ValueError(f"Unexpected app: {app}")
	return execute()
