from datetime import datetime
import unittest

from app.db.db_uploader import (
    missing_required_period_bounds,
    validate_required_period_bounds,
)
from app.server import complete_end_period, parse_header_date_with_type


class PeriodValidationTest(unittest.TestCase):
    def test_period_event_time_requires_start_and_end_period(self):
        with self.assertRaisesRegex(ValueError, "start_period and end_period"):
            validate_required_period_bounds("period", None, None)

    def test_period_event_time_accepts_start_period_without_end_period(self):
        self.assertEqual(
            missing_required_period_bounds("period", datetime(2026, 1, 1), None),
            [],
        )

    def test_non_period_event_time_does_not_require_header_period(self):
        validate_required_period_bounds("sale_date", None, None)

    def test_date_start_period_uses_same_date_as_end_period(self):
        start_period, is_datetime = parse_header_date_with_type("01.03.2026")

        end_period = complete_end_period("sale_date", start_period, is_datetime, None)

        self.assertEqual(end_period, datetime(2026, 3, 1))

    def test_datetime_start_period_uses_end_of_same_day_as_end_period(self):
        start_period, is_datetime = parse_header_date_with_type("01.03.2026 10:15:30")

        end_period = complete_end_period("period", start_period, is_datetime, None)

        self.assertEqual(end_period, datetime(2026, 3, 1, 23, 59, 59))


if __name__ == "__main__":
    unittest.main()
