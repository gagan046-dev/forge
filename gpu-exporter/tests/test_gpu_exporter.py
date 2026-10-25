"""Unit tests for the nvidia-smi CSV parsing and the two output formats.

Run from gpu-exporter/:  python -m unittest discover -s tests -v
"""

import os
import stat
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import gpu_exporter as ge  # noqa: E402

# Real output shape from the RTX 4070 Laptop GPU under WSL2 (values vary).
RTX_4070 = "NVIDIA GeForce RTX 4070 Laptop GPU, 37, 5123, 8188, 61, 42.17, 115.00\n"


class ParseCsvTest(unittest.TestCase):
    def test_single_gpu(self):
        [gpu] = ge.parse_csv(RTX_4070)
        self.assertEqual(gpu.index, 0)
        self.assertEqual(gpu.name, "NVIDIA GeForce RTX 4070 Laptop GPU")
        self.assertEqual(gpu.utilization_percent, 37.0)
        self.assertEqual(gpu.memory_used_bytes, 5123 * 1024 * 1024)
        self.assertEqual(gpu.memory_total_bytes, 8188 * 1024 * 1024)
        self.assertEqual(gpu.temperature_celsius, 61.0)
        self.assertEqual(gpu.power_watts, 42.17)
        self.assertEqual(gpu.power_limit_watts, 115.0)

    def test_not_available_values_become_none(self):
        # Laptop GPUs often cannot report power; nvidia-smi prints [N/A] / [Not Supported].
        [gpu] = ge.parse_csv("NVIDIA GeForce RTX 4070 Laptop GPU, 0, 512, 8188, 45, [N/A], [Not Supported]\n")
        self.assertIsNone(gpu.power_watts)
        self.assertIsNone(gpu.power_limit_watts)
        self.assertEqual(gpu.utilization_percent, 0.0)

    def test_multiple_gpus_keep_index_order_and_skip_blank_lines(self):
        out = "GPU A, 1, 2, 3, 4, 5, 6\n\nGPU B, 7, 8, 9, 10, 11, 12\r\n"
        gpus = ge.parse_csv(out)
        self.assertEqual([g.index for g in gpus], [0, 1])
        self.assertEqual([g.name for g in gpus], ["GPU A", "GPU B"])

    def test_wrong_field_count_is_an_error(self):
        with self.assertRaises(ValueError):
            ge.parse_csv("GPU A, 1, 2\n")

    def test_empty_output_gives_no_gpus(self):
        self.assertEqual(ge.parse_csv(""), [])


class RenderTest(unittest.TestCase):
    def test_metrics_text(self):
        text = ge.render_metrics(ge.parse_csv(RTX_4070), up=True)
        labels = 'gpu="0",name="NVIDIA GeForce RTX 4070 Laptop GPU"'
        self.assertIn("forge_gpu_up 1\n", text)
        self.assertIn(f"forge_gpu_utilization_percent{{{labels}}} 37.0\n", text)
        self.assertIn(f"forge_gpu_memory_total_bytes{{{labels}}} {8188 * 1024 * 1024}\n", text)
        self.assertIn(f"forge_gpu_power_watts{{{labels}}} 42.17\n", text)
        self.assertIn("# TYPE forge_gpu_temperature_celsius gauge\n", text)

    def test_missing_values_are_omitted_not_zero(self):
        gpus = ge.parse_csv("G, 5, 1, 2, 40, [N/A], [N/A]\n")
        text = ge.render_metrics(gpus, up=True)
        self.assertNotIn('forge_gpu_power_watts{', text)

    def test_down_exports_only_up_zero(self):
        text = ge.render_metrics([], up=False)
        self.assertIn("forge_gpu_up 0\n", text)
        self.assertNotIn("forge_gpu_utilization_percent{", text)

    def test_label_escaping(self):
        gpus = ge.parse_csv('"Odd \\ ""GPU""", 1, 2, 3, 4, 5, 6\n')
        text = ge.render_metrics(gpus, up=True)
        self.assertIn('name="Odd \\\\ \\"GPU\\""', text)

    def test_json_document(self):
        doc = ge.render_json(ge.parse_csv(RTX_4070), up=True, updated_at=0.0 + 1_700_000_000, error=None)
        self.assertTrue(doc["up"])
        self.assertEqual(len(doc["gpus"]), 1)
        self.assertEqual(doc["name"], "NVIDIA GeForce RTX 4070 Laptop GPU")
        self.assertEqual(doc["memory_used_bytes"], 5123 * 1024 * 1024)
        self.assertTrue(doc["updated_at"].startswith("2023-11-14T"))

    def test_json_when_down(self):
        doc = ge.render_json([], up=False, updated_at=None, error="boom")
        self.assertEqual(doc, {"up": False, "updated_at": None, "error": "boom", "gpus": []})


class CollectorErrorTest(unittest.TestCase):
    def test_missing_binary_is_reported_not_raised(self):
        collector = ge.Collector("/nonexistent/nvidia-smi", poll_seconds=5)
        collector.poll_once()
        _, up, _, error = collector.snapshot()
        self.assertFalse(up)
        self.assertTrue(error)


@unittest.skipIf(os.name == "nt", "uses a POSIX shell script as a fake nvidia-smi")
class CollectorTest(unittest.TestCase):
    def _fake_smi(self, body: str) -> str:
        fd, path = tempfile.mkstemp(suffix=".sh")
        with os.fdopen(fd, "w") as f:
            f.write("#!/bin/sh\n" + body + "\n")
        os.chmod(path, stat.S_IRWXU)
        self.addCleanup(os.remove, path)
        return path

    def test_poll_success_then_failure(self):
        ok = self._fake_smi(f"printf '{RTX_4070.strip()}\\n'")
        collector = ge.Collector(ok, poll_seconds=5)
        collector.poll_once()
        samples, up, updated_at, error = collector.snapshot()
        self.assertTrue(up)
        self.assertIsNone(error)
        self.assertEqual(samples[0].temperature_celsius, 61.0)

        collector.nvidia_smi = self._fake_smi("echo 'Failed to initialize NVML' >&2; exit 9")
        collector.poll_once()
        samples, up, _, error = collector.snapshot()
        self.assertFalse(up)
        self.assertEqual(samples, [])
        self.assertIn("exited 9", error)



if __name__ == "__main__":
    unittest.main()
