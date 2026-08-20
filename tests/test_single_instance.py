"""Tests for duplicate-start protection."""

import unittest
import uuid

from tracker.single_instance import SingleInstanceGuard


class TestSingleInstanceGuard(unittest.TestCase):

    def test_second_guard_is_rejected_until_first_releases(self):
        mutex_name = f"Local\\WorkTimeTracker.Test.{uuid.uuid4()}"
        first = SingleInstanceGuard(mutex_name)
        second = SingleInstanceGuard(mutex_name)

        try:
            self.assertTrue(first.acquire())
            self.assertFalse(second.acquire())
        finally:
            second.release()
            first.release()

        replacement = SingleInstanceGuard(mutex_name)
        try:
            self.assertTrue(replacement.acquire())
        finally:
            replacement.release()


if __name__ == "__main__":
    unittest.main()
