from memory_bench_platform.cli import build_parser


def test_build_parser_exposes_expected_subcommands():
    parser = build_parser()
    choices = parser._subparsers._group_actions[0].choices
    assert {"list-skills", "plan-run", "run", "validate", "analyze-run", "score-run"} <= set(choices)
