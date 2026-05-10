#!/usr/bin/env python
"""Verify submit-check retry policy.

Policy:
- If any non-ALREADY_SUBMITTED check has already failed, do not wait for
  pending self-correlation. The alpha is final RED/1INFERIOR.
- If the base checks have not failed and self-correlation is pending, retry so
  a true 7-pass candidate can receive its correlation result before labeling.
"""

from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import brain_client


class FakeResponse:
    status_code = 200

    def __init__(self, payload: dict):
        self.payload = payload

    def json(self) -> dict:
        return self.payload


class FakeSession:
    def __init__(self, payloads: list[dict]):
        self.payloads = payloads
        self.calls = 0

    def get(self, url: str) -> FakeResponse:
        self.calls += 1
        index = min(self.calls - 1, len(self.payloads) - 1)
        return FakeResponse(self.payloads[index])


def preview(checks: list[dict]) -> dict:
    return {"is": {"checks": checks}}


def main() -> None:
    failed_pending = preview(
        [
            {"name": "LOW_SHARPE", "result": "FAIL"},
            {"name": "SELF_CORRELATION", "result": "PENDING"},
        ]
    )
    seven_pass_pending = preview(
        [
            *[{"name": f"C{i}", "result": "PASS"} for i in range(7)],
            {"name": "SELF_CORRELATION", "result": "PENDING"},
        ]
    )
    resolved = preview(
        [
            *[{"name": f"C{i}", "result": "PASS"} for i in range(7)],
            {"name": "SELF_CORRELATION", "result": "PASS"},
        ]
    )

    with patch.object(brain_client, "sleep", lambda seconds: None):
        failed_session = FakeSession([failed_pending])
        brain_client.fetch_submit_checks_preview_with_retry(
            failed_session,
            "failed_alpha",
            max_retries=6,
            retry_delay_seconds=1,
        )
        if failed_session.calls != 1:
            raise AssertionError(
                f"failed alpha retried pending correlation: {failed_session.calls} calls"
            )

        pending_session = FakeSession([seven_pass_pending, seven_pass_pending, resolved])
        brain_client.fetch_submit_checks_preview_with_retry(
            pending_session,
            "seven_pass_alpha",
            max_retries=6,
            retry_delay_seconds=1,
        )
        if pending_session.calls != 3:
            raise AssertionError(
                f"7-pass pending alpha did not retry as expected: {pending_session.calls} calls"
            )

    print("check_policy_ok")


if __name__ == "__main__":
    main()
