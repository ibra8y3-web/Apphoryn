import unittest

from app.import_pacing import ImportPacer


class FakeClock:
    def __init__(self, now=0.0):
        self.now = float(now)

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


class ImportPacingTests(unittest.TestCase):
    def test_first_import_is_immediate_and_later_imports_are_one_minute_apart(self):
        clock = FakeClock()
        pacer = ImportPacer(60, clock=clock.monotonic, sleeper=clock.sleep)

        self.assertTrue(pacer.wait_for_slot())
        self.assertEqual(clock.now, 0)
        self.assertTrue(pacer.wait_for_slot())
        self.assertEqual(clock.now, 60)
        self.assertTrue(pacer.wait_for_slot())
        self.assertEqual(clock.now, 120)

    def test_deadline_preserves_control_when_next_slot_is_too_far_away(self):
        clock = FakeClock()
        pacer = ImportPacer(60, clock=clock.monotonic, sleeper=clock.sleep)

        self.assertTrue(pacer.wait_for_slot())
        self.assertFalse(pacer.wait_for_slot(deadline=20))
        self.assertEqual(clock.now, 20)

    def test_interval_can_be_disabled_for_explicit_zero_configuration(self):
        clock = FakeClock()
        pacer = ImportPacer(0, clock=clock.monotonic, sleeper=clock.sleep)

        self.assertTrue(pacer.wait_for_slot())
        self.assertTrue(pacer.wait_for_slot())
        self.assertEqual(clock.now, 0)


if __name__ == "__main__":
    unittest.main()
