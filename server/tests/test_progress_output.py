import unittest

from utils.progress_output import ProgressSampler, parse_progress_line


class ProgressOutputTest(unittest.TestCase):
    def test_rich_and_tqdm_records_ignore_timing_and_bar_width(self):
        a = parse_progress_line("translate ━━━━         42/100 0:00:01 0:00:03")
        b = parse_progress_line("translate ━━━━━━━━━━   42/100 0:00:08 0:00:06")
        self.assertEqual(a, b)
        self.assertEqual(a.text, "translate 42/100")
        step = parse_progress_line("Translate Paragraphs (2/3) ━━━ 2/10 0:00:02 -:--:--")
        self.assertEqual(step.text, "Translate Paragraphs (2/3) 2/10")
        self.assertFalse(step.overall)
        tqdm = parse_progress_line("translate: 50%|#####     | 1/2 [00:01<00:01, 1.2it/s]")
        self.assertEqual(tqdm.text, "translate 1/2")

    def test_progress_fractions_in_diagnostics_are_not_progress(self):
        for line in (
            "WARNING translate 9/10 failed", "ValueError: translate 9/10",
            "INFO Translate Paragraphs (1/1) 9/10", "translate 9/10 HTTP error",
            "WARNING Stage Translate Paragraphs completed with 2/3 items",
            '[PDF2ZH_DIAGNOSTIC] {"message":"translate 9/10"}',
            "ERROR https://example.test/translate/9/10", "private paragraph",
        ):
            with self.subTest(line=line):
                self.assertIsNone(parse_progress_line(line))

    def test_latest_task_progress_updates_without_waiting_for_console(self):
        clock = [0.0]
        sampler = ProgressSampler(clock=lambda: clock[0])
        sampler.update(parse_progress_line("translate 0/100"))
        self.assertEqual(sampler.snapshot(), "translate 0/100")
        clock[0] = 0.1
        update = sampler.update(parse_progress_line("translate 42/100"))
        self.assertEqual(update["progress"], 42)
        self.assertEqual(sampler.snapshot(), "")
        clock[0] = 1.9
        self.assertEqual(sampler.snapshot(), "")
        clock[0] = 2.0
        self.assertEqual(sampler.snapshot(), "translate 42/100")
        clock[0] = 10.0
        self.assertEqual(sampler.snapshot(), "")

    def test_old_completed_rows_do_not_replace_current_stage(self):
        sampler = ProgressSampler()
        complete = parse_progress_line("Parse Page Layout (1/1) 4/4")
        sampler.update(complete)
        current = parse_progress_line("Translate Paragraphs (1/1) 1/23")
        self.assertIn("Translate Paragraphs", sampler.update(current)["message"])
        self.assertIsNone(sampler.update(complete))
        self.assertEqual(sampler.stage, current)
        self.assertIn("Translate Paragraphs", sampler.snapshot(force=True))
        self.assertIsNone(sampler.update(current))

    def test_part_transition_resets_stage_counter(self):
        sampler = ProgressSampler()
        sampler.update(parse_progress_line("Translate Paragraphs (1/2) 23/23"))
        update = sampler.update(parse_progress_line("Translate Paragraphs (2/2) 0/23"))
        self.assertEqual(update["message"], "Translate Paragraphs (2/2) 0/23")

    def test_exit_snapshot_preserves_incomplete_or_complete_progress(self):
        for current in (23, 100):
            sampler = ProgressSampler(clock=lambda: 0)
            sampler.update(parse_progress_line("translate 0/100"))
            sampler.snapshot()
            update = sampler.update(parse_progress_line(f"translate {current}/100"))
            self.assertEqual(update["progress"], min(current, 99))
            self.assertEqual(sampler.snapshot(), "")
            self.assertEqual(sampler.snapshot(force=True), f"translate {current}/100")
            self.assertEqual(sampler.snapshot(force=True), "")

    def test_concurrent_tasks_have_independent_sampling_windows(self):
        first, second = ProgressSampler(clock=lambda: 0), ProgressSampler(clock=lambda: 0)
        for sampler, count in ((first, 3), (second, 7)):
            sampler.update(parse_progress_line(f"translate {count}/10"))
            self.assertEqual(sampler.snapshot(), f"translate {count}/10")

    def test_zero_total_does_not_divide_or_claim_success(self):
        sampler = ProgressSampler()
        self.assertNotIn("progress", sampler.update(parse_progress_line("translate 0/0")))
        self.assertEqual(sampler.snapshot(), "translate 0/0")


if __name__ == '__main__':
    unittest.main()
