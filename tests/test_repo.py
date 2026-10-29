import json
import os
import re
import subprocess
import sys
import unittest

from _load import ROOT

# the India edition: the original eleven, Pelosi replaced by the insider tracker, plus market and fno
SKILLS = ["trader-market", "trader-scanner", "trader-news", "trader-entry", "trader-exit", "trader-timemachine",
          "trader-fno", "trader-risk", "trader-budget", "trader-broker", "trader-alerts", "trader-analyst",
          "trader-insider", "trader-live"]
ABOUT = ("14 Claude skills for trading Indian markets (NSE/BSE): scan charts, track filings and news, time entries "
         "and exits, read option chains, replay outcomes with real Indian charges, paper trade on real-time Upstox "
         "data, and go live only when you approve it.")
# skills on the way to an order carry the safety block
ORDER_PATH = ["trader-scanner", "trader-entry", "trader-exit", "trader-timemachine", "trader-fno", "trader-budget",
              "trader-insider", "trader-risk", "trader-broker", "trader-alerts", "trader-live"]
US_ONLY = ["trader-pelosi", "trader-copycat", "trader-screener", "trader-tax", "trader-rebalance",
           "NYSE_HOLIDAYS", "eastern_offset", "nasdaqtrader", "SEC_USER_AGENT", "pdt_guard", "max_loss_per_trade_usd"]


def frontmatter(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    m = re.match(r"^---\n(.*?)\n---\n", text, re.S)
    return (m.group(1) if m else ""), text


def every_text_file():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in (".git", "__pycache__")]
        for f in files:
            if f.endswith((".md", ".py", ".json", ".csv", ".txt", ".xml")) or f == "LICENSE":
                yield os.path.join(base, f)


class Repo(unittest.TestCase):
    def test_skill_folders_at_the_root(self):
        found = sorted(d for d in os.listdir(ROOT) if os.path.isfile(os.path.join(ROOT, d, "SKILL.md")))
        self.assertEqual(found, sorted(SKILLS))

    def test_root_layout(self):
        root = sorted(d for d in os.listdir(ROOT) if not d.startswith(".git") and d != "__pycache__")
        self.assertEqual(root, sorted([".claude-plugin", "LICENSE", "README.md", "docs", "tests"] + SKILLS))

    def test_frontmatter_name_and_description(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertIn("name: %s\n" % s, fm + "\n", s)
            self.assertRegex(fm, r"description: ", s)
            self.assertGreater(len(fm), 200, "%s description is too thin" % s)
            self.assertGreater(len(text.splitlines()), 60, s)
            self.assertNotIn("<", fm, "%s: no angle brackets in frontmatter" % s)

    def test_order_path_skills_carry_the_safety_rule(self):
        for s in ORDER_PATH:
            _, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            low = " ".join(text.lower().split())
            self.assertIn("not financial advice", low, s)
            self.assertIn("yes", low, s)

    def test_every_skill_is_india_specific(self):
        for s in SKILLS:
            fm, text = frontmatter(os.path.join(ROOT, s, "SKILL.md"))
            self.assertRegex(text, r"NSE|BSE|NIFTY|India", s)

    def test_plugin_manifest_points_at_the_root_skills(self):
        with open(os.path.join(ROOT, ".claude-plugin", "plugin.json")) as fh:
            plugin = json.load(fh)
        self.assertEqual(plugin["name"], "day-trader-skill-india")
        self.assertEqual(plugin["skills"], "./")
        self.assertEqual(plugin["description"], ABOUT)
        with open(os.path.join(ROOT, ".claude-plugin", "marketplace.json")) as fh:
            market = json.load(fh)
        self.assertEqual(market["name"], "day-trader-skill-india")
        self.assertEqual(market["plugins"][0]["name"], "day-trader-skill-india")

    def test_readme_lists_every_skill_and_the_fine_print(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            readme = " ".join(fh.read().split())
        self.assertTrue(readme.startswith("# The Day Trader skill, India edition"))
        for line in (ABOUT, "## Fine print", "Not financial advice", "paper"):
            self.assertIn(line, readme)
        for s in SKILLS:
            self.assertIn("/" + s, readme, s)

    def test_docs_exist(self):
        for f in ("CHANGES-INDIA.md", "SETUP.md", "INDIA_RULES.md", "LIMITATIONS.md", "CONTRACTS.md", "LIVE.md"):
            self.assertTrue(os.path.isfile(os.path.join(ROOT, "docs", f)), f)

    def test_no_em_dashes_and_no_personal_email(self):
        for path in every_text_file():
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = fh.read()
            rel = os.path.relpath(path, ROOT)
            if not rel.startswith(os.path.join("tests", "fixtures")):
                self.assertNotIn("\u2014", text, rel)
            self.assertNotIn("jakeschincariol" + "@" + "gmail.com", text, rel)

    def test_no_us_market_leftovers_in_tools(self):
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith((".py", ".json", ".md")):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for old in US_ONLY:
                    self.assertNotIn(old, src, "%s/%s still says %s" % (s, f, old))

    def test_tools_import_the_shared_core(self):
        for s in SKILLS:
            if s == "trader-market":
                continue
            for f in os.listdir(os.path.join(ROOT, s)):
                if f.endswith(".py"):
                    with open(os.path.join(ROOT, s, f), encoding="utf-8") as fh:
                        self.assertIn("import market as M", fh.read(), "%s/%s" % (s, f))

    def test_every_tool_runs_on_python_3_8(self):
        for s in SKILLS:
            folder = os.path.join(ROOT, s)
            for f in os.listdir(folder):
                if not f.endswith(".py"):
                    continue
                with open(os.path.join(folder, f), encoding="utf-8") as fh:
                    src = fh.read()
                for bad in ("import zoneinfo", "from zoneinfo", ".removeprefix(", ".removesuffix("):
                    self.assertNotIn(bad, src, "%s/%s" % (s, f))
                self.assertNotRegex(src, r"(?m)^\s+match \w+.*:$", "%s/%s" % (s, f))
                for bad in ("list[", "dict[", "tuple[", "set["):
                    self.assertNotRegex(src, r"(?<![\w.\"'])%s(?!\])" % re.escape(bad), "%s/%s" % (s, f))
                self.assertNotRegex(src, r"\w+ \| None", "%s/%s" % (s, f))

    def test_every_tool_shows_help(self):
        for s in SKILLS:
            for f in os.listdir(os.path.join(ROOT, s)):
                if f.endswith(".py"):
                    p = subprocess.run([sys.executable, os.path.join(ROOT, s, f), "--help"],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
                    self.assertEqual(p.returncode, 0, "%s/%s --help: %s" % (s, f, p.stderr.decode()[-300:]))

    def test_example_config_is_india_and_safe(self):
        with open(os.path.join(ROOT, "trader-budget", "config.example.json")) as fh:
            cfg = json.load(fh)
        for k in ("max_loss_per_trade_inr", "max_daily_loss_inr", "max_trades_per_day", "min_reward_risk",
                  "require_stop", "no_entries_after", "flat_by", "broker"):
            self.assertIn(k, cfg, k)
        self.assertIs(cfg["require_stop"], True)
        self.assertIs(cfg["paper"], True)
        self.assertIs(cfg.get("allow_short", False), False)
        self.assertIs(cfg.get("allow_option_selling", False), False)
        self.assertIs(cfg.get("allow_futures", False), False)
        self.assertLess(cfg["flat_by"], "15:20")


if __name__ == "__main__":
    unittest.main()
