import unittest

from app.db.errors import build_load_error


class BuildLoadErrorTest(unittest.TestCase):
    def test_executemany_type_mismatch_points_to_column_and_row(self):
        exc = Exception(
            "invalid input for query argument $2 in element #4 of executemany() sequence: "
            "1.0 (expected str, got float)"
        )

        err = build_load_error(exc, file_path="f.txt", table_name="t", columns=["a", "b"])

        self.assertEqual(err.column, "b")
        self.assertEqual(err.row_number, 5)
        self.assertEqual(err.expected, "str")

    def test_copy_style_message_has_no_row_number(self):
        exc = Exception("invalid input for query argument $1: 'x' (expected int, got str)")

        err = build_load_error(exc, columns=["a"])

        self.assertEqual(err.column, "a")
        self.assertIsNone(err.row_number)

    def test_unknown_error_falls_back_to_generic_message(self):
        err = build_load_error(RuntimeError("boom"), file_path="f.txt")

        self.assertEqual(err.message, "boom")
        self.assertIn("f.txt", err.format_full())


if __name__ == "__main__":
    unittest.main()
