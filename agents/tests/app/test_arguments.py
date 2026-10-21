"""The argument validator returns argv and refuses what the launcher owns."""

import builtins
import copy
import json
import sys
import unittest
from pathlib import Path

AGENTS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(AGENTS))

from matuto import arguments  # noqa: E402  (agents/ carries the package)
from matuto_runtime import _parsed_arguments  # noqa: E402  (the runtime's own option view)

# One spelling the runtime accepts each: the long form, a short alias or an
# inline value.
SPELLINGS = {"--rpc": ["--rpc", "--rpc=127.0.0.1"],
             "--api-key": ["--api-key", "--api-key=secret"],
             "--config": ["--config", "--config=/etc/llama.cpp/config.ini"],
             "--preset": ["--preset", "--preset=default"],
             "--override-tensor": ["--override-tensor", "-ot", "--override-tensor=CPU"],
             "--model": ["--model", "-m", "--model=/srv/weights/model.gguf"],
             "--host": ["--host", "--host=0.0.0.0"],
             "--port": ["--port", "--port=8080"]}


def rows_from(tokens, tool):
    """Argument rows for a synthetic tool invocation, in row order."""
    rows = []
    for token in tokens:
        if token.startswith("-") or not rows or rows[-1]["value"]:
            rows.append({"tool": tool, "option": token, "value": ""})
        else:
            rows[-1]["value"] = token
    return rows


def help_for(rows):
    """The help a build would print for the flags these rows use."""
    return "".join(f"  {row['option']} FNAME\n" if row["value"] else f"  {row['option']}\n"
                   for row in {row["option"]: row for row in rows}.values())


def catalogue_for(rows):
    entries = {}
    for row in rows:
        entry = entries.setdefault(row["option"], {"flag": row["option"], "takesValue": False})
        entry["takesValue"] = entry["takesValue"] or bool(row["value"])
    return entries


class ProfileArgumentsTest(unittest.TestCase):
    def assertRefuses(self, rows, catalogue=(), selector=None, help_text=None):
        with self.assertRaises(arguments.Refusal) as raised:
            arguments.save_checks(rows, catalogue, selector=selector, help_text=help_text)
        return str(raised.exception)

    def assertNamesRow(self, message, tool, position, flag):
        self.assertIn(f"{tool} argument row {position}", message)
        self.assertIn(f"({flag})", message)
        self.assertTrue(message.split(": ", 1)[1], message)

    def test_custom_and_catalogue_rows_preserve_tool_argv(self):
        for tool, tokens in (
                ("server", ["--parallel", "1", "--cache-prompt"]),
                ("smoke", ["-p", "generic fixture prompt", "--single-turn"]),
                ("bench", ["--ctx-size=4096"])):
            with self.subTest(tool=tool, argv=tokens):
                rows = rows_from(tokens, tool)
                catalogue = list(catalogue_for(rows).values())
                as_custom = [{"tool": r["tool"], "option": r["option"], "value": r["value"]} for r in rows]
                as_catalogue = [{"tool": r["tool"], "catalogue": r["option"], "value": r["value"]} for r in rows]
                help_text = {tool: help_for(rows)}

                checked = arguments.save_checks(as_custom, selector="HIP0", help_text=help_text)

                self.assertEqual(checked["argv"][tool], tokens)
                self.assertEqual(checked["warnings"], [])
                self.assertEqual(set(checked["argv"]), {"server", "smoke", "bench"})
                self.assertEqual(arguments.save_checks(as_catalogue, catalogue, selector="HIP0",
                                                       help_text=help_text)["argv"][tool], tokens)

    def test_custom_value_is_one_argv_element_unchanged(self):
        value = "- two words 'quoted' \"double\" $(whoami); rm -rf / &|"
        checked = arguments.save_checks([{"tool": "smoke", "option": "-p", "value": value},
                                         {"tool": "smoke", "option": "--single-turn", "value": None}])

        self.assertEqual(checked["argv"]["smoke"], ["-p", value, "--single-turn"])

    def test_short_alias_keeps_its_advertised_meaning_for_duplicate_checks(self):
        rows = [{"tool": "server", "option": "-r", "value": "STOP"},
                {"tool": "server", "option": "--repeat-penalty", "value": "1.2"}]

        checked = arguments.save_checks(
            rows, help_text={"server": "-r, --reverse-prompt PROMPT\n--repeat-penalty N\n"})

        self.assertEqual(checked["argv"]["server"], ["-r", "STOP", "--repeat-penalty", "1.2"])

    def test_a_value_that_looks_like_an_option_stays_data(self):
        checked = arguments.save_checks([{"tool": "server", "option": "--alias", "value": "--rpc"}],
                                        help_text={"server": "--alias FNAME\n"})

        self.assertEqual(checked["argv"]["server"], ["--alias", "--rpc"])

    def test_an_accepted_value_never_becomes_another_option(self):
        # The runtime reads each element as an option or as data; an accepted row
        # must name exactly its own options and nothing more.
        rows = [{"tool": "server", "option": "--alias", "value": "--rpc"},
                {"tool": "server", "option": "-p", "value": "-a prompt"},
                {"tool": "server", "option": "--temp", "value": "-1"},
                {"tool": "server", "option": "--ctx-size=4096", "value": ""}]
        help_text = {"server": "--alias FNAME\n-p FNAME\n--temp FNAME\n--ctx-size N\n"}

        argv = arguments.save_checks(rows, selector="HIP0", help_text=help_text)["argv"]["server"]

        self.assertEqual([canonical for _, canonical, _ in _parsed_arguments(argv, help_text["server"])],
                         ["--alias", "-p", "--temp", "--ctx-size"])

    def test_a_value_the_build_takes_as_an_operand_is_data_even_with_a_leading_dash(self):
        # F2: --grammar-file is not in the runtime's flat value table, so no
        # arity guess can refuse this; the build's own help consumes the operand.
        rows = [{"tool": "server", "option": "--grammar-file", "value": "-rules.gbnf"}]
        help_text = {"server": "--grammar-file FNAME\n"}

        checked = arguments.save_checks(rows, help_text=help_text)
        self.assertEqual(checked["argv"]["server"], ["--grammar-file", "-rules.gbnf"])
        self.assertEqual(checked["warnings"], [])
        self.assertEqual(arguments.launch_check(rows, help_text=help_text)["argv"]["server"],
                         ["--grammar-file", "-rules.gbnf"])

    def test_a_value_of_an_unadvertised_flag_is_data_not_a_second_option(self):
        # An unadvertised flag has no known arity, so its value stays one argv
        # element; the flag itself still warns at save and refuses at launch.
        rows = [{"tool": "server", "option": "--brand-new", "value": "--rpc"}]

        saved = arguments.save_checks(rows, help_text={"server": "--jinja\n"})

        self.assertEqual(saved["argv"]["server"], ["--brand-new", "--rpc"])
        self.assertEqual(len(saved["warnings"]), 1)
        self.assertIn("--brand-new", saved["warnings"][0])
        with self.assertRaises(arguments.Refusal) as raised:
            arguments.launch_check(rows, help_text={"server": "--jinja\n"})
        self.assertIn("--brand-new", str(raised.exception))
        # Without help only the flag's own spelling is checked.
        self.assertEqual(arguments.save_checks([{"tool": "server", "option": "--grammar-file", "value": "--rpc"}])["argv"]["server"],
                         ["--grammar-file", "--rpc"])

    def test_an_unadvertised_flags_value_does_not_make_a_duplicate(self):
        rows = [{"tool": "server", "option": "--brand-new", "value": "--ctx-size"},
                {"tool": "server", "option": "--ctx-size", "value": "4096"}]

        checked = arguments.save_checks(rows, help_text={"server": "--jinja\n--ctx-size N\n"})

        self.assertEqual(checked["argv"]["server"], ["--brand-new", "--ctx-size", "--ctx-size", "4096"])
        self.assertEqual(len(checked["warnings"]), 1)
        self.assertIn("--brand-new", checked["warnings"][0])

    def test_a_value_that_would_be_read_as_an_option_is_refused(self):
        help_text = {"server": "--ctx-size N\n--jinja\n--grammar-file FNAME\n"}
        catalogue = [{"flag": "--jinja", "takesValue": True}]
        for row, protected in (({"option": "--ctx-size=4096", "value": "--rpc=127.0.0.1"}, "--rpc"),
                               ({"option": "--jinja", "value": "--host=0.0.0.0"}, "--host"),
                               ({"option": "--jinja", "value": "--rpc=127.0.0.1"}, "--rpc"),
                               ({"catalogue": "--jinja", "value": "--rpc=127.0.0.1"}, "--rpc")):
            with self.subTest(option=row.get("option", row.get("catalogue")), value=row["value"]):
                row = dict(row, tool="server")
                message = self.assertRefuses([row], catalogue, selector="HIP0", help_text=help_text)
                self.assertNamesRow(message, "server", 1, row.get("option") or row.get("catalogue"))
                self.assertIn(protected, message)
                self.assertRaises(arguments.Refusal, arguments.launch_check, [row], catalogue,
                                  selector="HIP0", help_text=help_text)

    def test_a_value_the_build_reads_as_another_option_refuses_even_when_advertised(self):
        # A Custom value must stay data. When the build reads it as a second
        # option, the row refuses whether or not that option is advertised.
        help_text = {"server": "--jinja\n--ctx-size N\n--metrics\n"}
        for row in ({"option": "--jinja", "value": "--metrics"},
                    {"option": "--ctx-size=4096", "value": "--metrics"}):
            with self.subTest(row=row):
                rows = [dict(row, tool="server")]
                message = self.assertRefuses(rows, help_text=help_text)
                self.assertNamesRow(message, "server", 1, row["option"])
                self.assertIn("--metrics", message)
                self.assertRaises(arguments.Refusal, arguments.launch_check, rows, help_text=help_text)

    def test_refused_and_launcher_owned_options_are_refused_as_every_row_kind_and_spelling(self):
        catalogue = [{"flag": canonical, "takesValue": True} for canonical in SPELLINGS]
        for canonical, spellings in SPELLINGS.items():
            reason = "protected execution contract" if canonical in arguments.REFUSED else "owned by the launcher"
            with self.subTest(option=canonical):
                for spelling in spellings:
                    with self.subTest(spelling=spelling):
                        message = self.assertRefuses([{"tool": "server", "option": spelling}])
                        self.assertNamesRow(message, "server", 1, spelling)
                        self.assertIn(reason, message)
                message = self.assertRefuses([{"tool": "server", "catalogue": canonical, "value": "1"}], catalogue)
                self.assertNamesRow(message, "server", 1, canonical)
                self.assertIn(reason, message)

    def test_a_catalogue_row_may_only_carry_a_value_the_entry_takes(self):
        catalogue = [{"flag": "--single-turn", "takesValue": False}, {"flag": "--parallel", "takesValue": True}]

        self.assertEqual(arguments.save_checks([{"tool": "server", "catalogue": "--parallel", "value": "1"}],
                                               catalogue)["argv"]["server"], ["--parallel", "1"])
        self.assertIn("takes no value", self.assertRefuses([{"tool": "server", "catalogue": "--single-turn", "value": "1"}], catalogue))

    def test_a_row_naming_no_catalogue_entry_refuses(self):
        self.assertIn("no argument-catalogue entry", self.assertRefuses([{"tool": "bench", "catalogue": "--n-gpu-layers", "value": "99"}]))
        self.assertIn("neither a catalogue entry nor a Custom flag", self.assertRefuses([{"tool": "bench", "value": "99"}]))

    def test_a_value_taking_row_without_a_value_refuses(self):
        # A missing operand would let the runtime read the next row's flag as
        # this row's value, hiding that flag from the option checks.
        catalogue = [{"flag": "--n-cpu-moe", "takesValue": True}]
        help_text = {"server": "--n-cpu-moe N\n--grammar-file FNAME\n--device SEL\n"}
        for row in ({"option": "--n-cpu-moe"}, {"catalogue": "--n-cpu-moe"}):
            with self.subTest(row=row):
                rows = [dict(row, tool="server", value=""),
                        {"tool": "server", "option": "--grammar-file", "value": "--device=Vulkan0"}]
                message = self.assertRefuses(rows, catalogue, selector="HIP0", help_text=help_text)
                self.assertNamesRow(message, "server", 1, "--n-cpu-moe")
                self.assertIn("carries none", message)

    def test_a_device_option_must_name_the_card_the_launcher_selects(self):
        help_text = {"server": "--device SEL\n--device-draft SEL\n--mmproj-device SEL\n--main-gpu N\n--tensor-split LIST\n"}
        for flag in ("--device", "--device=HIP0", "-dev=HIP0"):
            with self.subTest(flag=flag):
                self.assertEqual(arguments.save_checks([{"tool": "server", "option": flag, "value": "" if "=" in flag else "HIP0"}],
                                                       selector="HIP0", help_text=help_text)["argv"]["server"],
                                 [flag] + ([] if "=" in flag else ["HIP0"]))
        for option in ("--device", "-dev", "--device-draft", "-devd", "--spec-draft-device", "--mmproj-device", "-mmdev"):
            with self.subTest(option=option):
                message = self.assertRefuses([{"tool": "server", "option": option, "value": "Vulkan0"}], selector="HIP0", help_text=help_text)
                self.assertIn("conflicts with the card", message)
                self.assertNamesRow(message, "server", 1, option)
        self.assertIn("conflicts with the card", self.assertRefuses([{"tool": "server", "option": "--device=Vulkan0", "value": ""}],
                                                                    selector="HIP0", help_text=help_text))
        for option, value in (("--main-gpu", "1"), ("--tensor-split", "1,1"), ("-mg", "0.5")):
            with self.subTest(option=option, value=value):
                message = self.assertRefuses([{"tool": "server", "option": option, "value": value}], selector="HIP0", help_text=help_text)
                self.assertIn("single selected GPU", message)
                self.assertNamesRow(message, "server", 1, option)

    def test_a_duplicate_option_within_one_tool_refuses(self):
        catalogue = [{"flag": "--parallel", "takesValue": True}]
        help_text = {"server": "--parallel N\n--ctx-size N\n"}
        message = self.assertRefuses([{"tool": "server", "catalogue": "--parallel", "value": "1"},
                                      {"tool": "server", "option": "--parallel", "value": "2"}], catalogue, help_text=help_text)
        self.assertIn("already set for server", message)
        self.assertNamesRow(message, "server", 2, "--parallel")
        message = self.assertRefuses([{"tool": "server", "option": "-c", "value": "4096"},
                                      {"tool": "server", "option": "--ctx-size=8192"}], help_text=help_text)
        self.assertIn("already set", message)
        self.assertNamesRow(message, "server", 2, "--ctx-size=8192")
        # The same option on two tools is two launches, not a duplicate.
        self.assertEqual(arguments.save_checks([{"tool": "server", "option": "--jinja"},
                                                {"tool": "smoke", "option": "--jinja"}])["argv"]["server"],
                         ["--jinja"])

    def test_an_inline_value_stays_in_the_one_flag_element(self):
        checked = arguments.save_checks([{"tool": "bench", "option": "--ctx-size=8192", "value": ""}],
                                        selector="HIP0", help_text={"bench": "--ctx-size N\n"})

        self.assertEqual(checked["argv"]["bench"], ["--ctx-size=8192"])

    def test_an_unadvertised_option_warns_at_save_and_is_refused_at_launch(self):
        help_text = {"server": "--parallel N\n--cache-prompt\n"}
        rows = [{"tool": "server", "option": "--parallel", "value": "1"},
                {"tool": "server", "option": "--flash-attn", "value": "on"}]

        saved = arguments.save_checks(rows, help_text=help_text)

        self.assertEqual(saved["argv"]["server"], ["--parallel", "1", "--flash-attn", "on"])
        self.assertEqual(len(saved["warnings"]), 1)
        self.assertIn("--flash-attn", saved["warnings"][0])
        with self.assertRaises(arguments.Refusal) as raised:
            arguments.launch_check(rows, help_text=help_text)
        self.assertIn("not advertised by the active build", str(raised.exception))

    def test_a_row_is_judged_against_the_spelling_the_build_advertises(self):
        # The build's help lists the spellings it accepts, and the runtime
        # compares a row against them as written, so the validator does too.
        rows = [{"tool": "server", "option": "-c", "value": "4096"}]

        self.assertEqual(arguments.save_checks(rows, help_text={"server": "-c N\n--ctx-size N\n"})["warnings"], [])
        self.assertEqual(len(arguments.save_checks(rows, help_text={"server": "--ctx-size N\n"})["warnings"]), 1)
        self.assertRaises(arguments.Refusal, arguments.launch_check, rows, help_text={"server": "--ctx-size N\n"})

    def test_the_launch_check_refuses_when_the_advertised_options_are_unavailable(self):
        rows = [{"tool": "server", "option": "--jinja"}]
        for help_text in (None, {}, {"server": ""}, {"smoke": "--jinja\n"}, ["--jinja"]):
            with self.subTest(help_text=help_text):
                with self.assertRaises(arguments.Refusal) as raised:
                    arguments.launch_check(rows, help_text=help_text)
                self.assertIn("server", str(raised.exception))
                self.assertIn("unavailable", str(raised.exception))

    def test_the_launch_check_passes_a_profile_whose_tool_is_advertised(self):
        checked = arguments.launch_check([{"tool": "server", "option": "--jinja"}], help_text={"server": "--jinja\n"})

        self.assertEqual(checked["argv"]["server"], ["--jinja"])
        self.assertEqual(checked["warnings"], [])

    def test_the_validator_reads_no_file_and_changes_no_record(self):
        rows = [{"tool": "bench", "option": "--n-cpu-moe", "value": "30"}]
        untouched = copy.deepcopy(rows)
        opened = []
        opening = builtins.open

        def watched(*positional, **keywords):
            opened.append(positional[0])
            return opening(*positional, **keywords)

        builtins.open = watched
        self.addCleanup(setattr, builtins, "open", opening)
        arguments.save_checks(rows, catalogue_for(rows).values(), selector="HIP0",
                              help_text={"bench": "--n-cpu-moe N\n"})

        self.assertEqual(opened, [])
        self.assertEqual(json.loads(json.dumps(rows)), untouched)


if __name__ == "__main__":
    unittest.main()
