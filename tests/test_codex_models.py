import json
import tempfile
import unittest
from pathlib import Path
from tracker.ai_token_reader import _read_codex_file_daily_tokens


class CodexModelTests(unittest.TestCase):
    def read(self, events):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/"rollout-2026-11-25T10-00-00-test.jsonl"
            path.write_text("\n".join(json.dumps({"timestamp":"2026-11-25T02:00:00Z",**event}) for event in events),encoding="utf-8")
            return _read_codex_file_daily_tokens(path)["2026-11-25"]

    def usage(self, inp, cached, out):
        return {"type":"event_msg","payload":{"type":"token_count","info":{"total_token_usage":{"input_tokens":inp,"cached_input_tokens":cached,"output_tokens":out}}}}

    def context(self, model):
        return {"type":"turn_context","payload":{"model":model}}

    def user(self):
        return {"type":"response_item","payload":{"type":"message","role":"user","content":[{"type":"input_text","text":"Hello"}]}}

    def test_actual_context_model_without_provenance(self):
        result=self.read([{"type":"session_meta","payload":{}},self.user(),self.context("gpt-6.1-sol"),self.usage(100,60,10)])
        self.assertEqual(list(result),["gpt-6.1-sol"])
        self.assertEqual(result["gpt-6.1-sol"],{"input":40,"cached":60,"output":10,"sessions":1,"messages":1})

    def test_switch_model_keeps_cumulative_counter_and_messages(self):
        result=self.read([self.user(),self.context("model-a"),self.usage(100,60,10),self.user(),self.context("model-b"),self.usage(170,100,30)])
        self.assertEqual(result["model-a"]["input"],40)
        self.assertEqual(result["model-b"]["input"],30)
        self.assertEqual(result["model-b"]["cached"],40)
        self.assertEqual(result["model-b"]["output"],20)
        self.assertEqual(sum(r["input"]+r["cached"]+r["output"] for r in result.values()),200)
        self.assertEqual(result["model-a"]["messages"],1)
        self.assertEqual(result["model-b"]["messages"],1)

    def test_duplicate_usage_not_duplicated_after_model_switch(self):
        result=self.read([self.context("model-a"),self.usage(100,60,10),self.context("model-b"),self.usage(100,60,10)])
        self.assertNotIn("model-b",result)

    def test_unknown_is_not_application_name(self):
        self.assertEqual(list(self.read([self.usage(100,60,10)])),["unknown"])

    def test_legacy_provenance_supported(self):
        result=self.read([{"type":"session_meta","payload":{"base_instructions":{"provenance":{"model":"old-model"}}}},self.usage(100,60,10)])
        self.assertIn("old-model",result)


if __name__=="__main__": unittest.main()
